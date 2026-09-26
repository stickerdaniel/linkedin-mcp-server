import asyncio
import logging
from typing import Any, Callable, Coroutine, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastmcp import FastMCP
from fastmcp.tools import FunctionTool

from linkedin_mcp_server.callbacks import MCPContextProgressCallback
from linkedin_mcp_server.scraping.contracts import (
    RATE_LIMITED_SECTION_TEXT,
    SEND_INTERRUPTED_WARNING,
)
from linkedin_mcp_server.scraping.contracts import ExtractedSection


async def get_tool_fn(
    mcp: FastMCP, name: str
) -> Callable[..., Coroutine[Any, Any, dict[str, Any]]]:
    """Extract tool function from FastMCP by name using public API."""
    tool = await mcp.get_tool(name)
    if tool is None:
        raise ValueError(f"Tool '{name}' not found")
    return cast(FunctionTool, tool).fn


def _make_mock_extractor(scrape_result: dict) -> MagicMock:
    """Create a mock LinkedInExtractor that returns the given result."""
    mock = MagicMock()
    mock.scrape_person = AsyncMock(return_value=scrape_result)
    mock.connect_with_person = AsyncMock(return_value=scrape_result)
    mock.scrape_company = AsyncMock(return_value=scrape_result)
    mock.scrape_job = AsyncMock(return_value=scrape_result)
    mock.search_jobs = AsyncMock(return_value=scrape_result)
    mock.get_saved_jobs = AsyncMock(return_value=scrape_result)
    mock.search_people = AsyncMock(return_value=scrape_result)
    mock.get_sidebar_profiles = AsyncMock(return_value=scrape_result)
    mock.get_inbox = AsyncMock(return_value=scrape_result)
    mock.get_conversation = AsyncMock(return_value=scrape_result)
    mock.search_conversations = AsyncMock(return_value=scrape_result)
    mock.send_message = AsyncMock(return_value=scrape_result)
    mock.get_my_profile = AsyncMock(return_value=scrape_result)
    mock.search_companies = AsyncMock(return_value=scrape_result)
    mock.search_posts = AsyncMock(return_value=scrape_result)
    mock.get_company_employees = AsyncMock(return_value=scrape_result)
    mock.extract_page = AsyncMock(
        return_value=ExtractedSection(text="some text", references=[])
    )
    mock.extract_feed = AsyncMock(return_value=ExtractedSection(text="", references=[]))
    return mock


_TOOL_MODULES = ("person", "company", "job", "messaging", "feed", "post")


@pytest.fixture
def serve_extractor(monkeypatch: pytest.MonkeyPatch) -> Callable[[Any], AsyncMock]:
    """Answer every tool's readiness call with the given extractor.

    Patched in each tool module, which is where the tools look it up, so a tool
    body runs unchanged from the readiness call onwards.
    """

    def serve(extractor: Any) -> AsyncMock:
        ready = AsyncMock(return_value=extractor)
        for module in _TOOL_MODULES:
            monkeypatch.setattr(
                f"linkedin_mcp_server.tools.{module}.get_ready_extractor", ready
            )
        return ready

    return serve


@pytest.mark.parametrize(
    ("module_name", "tool_name", "arguments", "error_match"),
    [
        (
            "person",
            "get_person_profile",
            {"linkedin_username": "/feed/"},
            "not a personal profile",
        ),
        (
            "person",
            "connect_with_person",
            {"linkedin_username": "/feed/"},
            "not a personal profile",
        ),
        (
            "person",
            "get_sidebar_profiles",
            {"linkedin_username": "/feed/"},
            "not a personal profile",
        ),
        (
            "person",
            "search_people",
            {"keywords": "engineer", "current_company": "SAP"},
            "numeric LinkedIn company URN id",
        ),
        (
            "company",
            "get_company_profile",
            {"company_name": "/feed/"},
            "not a company page",
        ),
        (
            "company",
            "get_company_posts",
            {"company_name": "/feed/"},
            "not a company page",
        ),
        (
            "company",
            "get_company_employees",
            {"company_name": "/feed/"},
            "not a company page",
        ),
        (
            "job",
            "get_job_details",
            {"job_id": "/feed/"},
            "job_id is not a LinkedIn id",
        ),
        (
            "messaging",
            "get_conversation",
            {"linkedin_username": "/feed/"},
            "not a personal profile",
        ),
        (
            "messaging",
            "get_conversation",
            {"thread_id": "/feed/"},
            "thread_id is not a LinkedIn id",
        ),
        (
            "messaging",
            "get_conversation",
            {"linkedin_username": "alice", "thread_id": "/feed/"},
            "thread_id is not a LinkedIn id",
        ),
        (
            "messaging",
            "send_message",
            {
                "linkedin_username": "/feed/",
                "message": "Hello",
                "confirm_send": False,
            },
            "not a personal profile",
        ),
        (
            "messaging",
            "send_message",
            {
                "linkedin_username": "alice",
                "profile_urn": "/feed/",
                "message": "Hello",
                "confirm_send": False,
            },
            "profile_urn is not a LinkedIn id",
        ),
        (
            "messaging",
            "send_message",
            {
                "linkedin_username": "alice",
                "profile_urn": "urn:li:company:123",
                "message": "Hello",
                "confirm_send": False,
            },
            "profile_urn is not a LinkedIn id",
        ),
        (
            "messaging",
            "send_message",
            {
                "linkedin_username": "alice",
                "profile_urn": "urn:li:fsd_profile:",
                "message": "Hello",
                "confirm_send": False,
            },
            "profile_urn is not a LinkedIn id",
        ),
    ],
)
async def test_invalid_reference_is_rejected_before_extractor(
    module_name, tool_name, arguments, error_match
):
    from fastmcp.exceptions import ToolError

    from linkedin_mcp_server.tools.company import register_company_tools
    from linkedin_mcp_server.tools.job import register_job_tools
    from linkedin_mcp_server.tools.messaging import register_messaging_tools
    from linkedin_mcp_server.tools.person import register_person_tools

    register_by_module = {
        "company": register_company_tools,
        "job": register_job_tools,
        "messaging": register_messaging_tools,
        "person": register_person_tools,
    }
    mcp = FastMCP("test")
    register_by_module[module_name](mcp)
    ready = AsyncMock(side_effect=AssertionError("get_ready_extractor was called"))

    with patch(f"linkedin_mcp_server.tools.{module_name}.get_ready_extractor", ready):
        with pytest.raises(ToolError, match=error_match):
            await mcp.call_tool(tool_name, arguments)

    ready.assert_not_awaited()


@pytest.mark.parametrize(
    ("module_name", "tool_name", "arguments"),
    [
        ("person", "get_person_profile", {"linkedin_username": "alice"}),
        ("person", "search_people", {"keywords": "engineer"}),
        ("person", "connect_with_person", {"linkedin_username": "alice"}),
        ("person", "get_sidebar_profiles", {"linkedin_username": "alice"}),
        ("person", "get_my_profile", {}),
        ("company", "get_company_profile", {"company_name": "anthropic"}),
        ("company", "get_company_posts", {"company_name": "anthropic"}),
        ("company", "search_companies", {"keywords": "fintech"}),
        ("company", "get_company_employees", {"company_name": "anthropic"}),
        ("job", "get_job_details", {"job_id": "4252026496"}),
        ("job", "search_jobs", {"keywords": "python"}),
        ("job", "get_saved_jobs", {}),
        ("messaging", "get_inbox", {}),
        ("messaging", "get_conversation", {"linkedin_username": "alice"}),
        ("messaging", "search_conversations", {"keywords": "hello"}),
        (
            "messaging",
            "send_message",
            {"linkedin_username": "alice", "message": "Hello", "confirm_send": False},
        ),
        ("feed", "get_feed", {}),
        ("post", "search_posts", {"keywords": "python"}),
    ],
)
async def test_valid_input_reaches_the_patched_readiness_call(
    module_name, tool_name, arguments
):
    # The counterpart of the test above. Without it, a patch on an attribute
    # the tool no longer reads would leave "never awaited" true for invalid
    # input and prove nothing about validation order.
    import importlib

    module = importlib.import_module(f"linkedin_mcp_server.tools.{module_name}")
    mcp = FastMCP("test")
    getattr(module, f"register_{module_name}_tools")(mcp)
    ready = AsyncMock(
        return_value=_make_mock_extractor(
            {"url": "https://www.linkedin.com/", "sections": {}}
        )
    )

    with patch(f"linkedin_mcp_server.tools.{module_name}.get_ready_extractor", ready):
        await mcp.call_tool(tool_name, arguments)

    ready.assert_awaited_once()
    assert ready.await_args is not None
    assert ready.await_args.kwargs == {"tool_name": tool_name}


class TestPersonTool:
    async def test_get_person_profile_success(self, mock_context, serve_extractor):
        expected = {
            "url": "https://www.linkedin.com/in/test-user/",
            "sections": {"main_profile": "John Doe\nSoftware Engineer"},
        }
        mock_extractor = _make_mock_extractor(expected)

        from linkedin_mcp_server.tools.person import register_person_tools

        mcp = FastMCP("test")
        register_person_tools(mcp)

        serve_extractor(mock_extractor)
        tool_fn = await get_tool_fn(mcp, "get_person_profile")
        result = await tool_fn(
            "https://de.linkedin.com/in/test-user/",
            mock_context,
        )
        assert result["url"] == "https://www.linkedin.com/in/test-user/"
        assert "main_profile" in result["sections"]
        assert "pages_visited" not in result
        assert "sections_requested" not in result
        assert mock_extractor.scrape_person.await_args.args[0] == "test-user"

    async def test_get_person_profile_with_sections(
        self, mock_context, serve_extractor
    ):
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

        serve_extractor(mock_extractor)
        tool_fn = await get_tool_fn(mcp, "get_person_profile")
        result = await tool_fn(
            "test-user",
            mock_context,
            sections="experience,contact_info",
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

    async def test_get_person_profile_passes_callbacks(
        self, mock_context, serve_extractor
    ):
        """Verify tool wires MCPContextProgressCallback to the extractor."""
        expected = {
            "url": "https://www.linkedin.com/in/test-user/",
            "sections": {"main_profile": "John Doe"},
        }
        mock_extractor = _make_mock_extractor(expected)

        from linkedin_mcp_server.tools.person import register_person_tools

        mcp = FastMCP("test")
        register_person_tools(mcp)

        serve_extractor(mock_extractor)
        tool_fn = await get_tool_fn(mcp, "get_person_profile")
        await tool_fn("test-user", mock_context)

        call_kwargs = mock_extractor.scrape_person.call_args.kwargs
        assert "callbacks" in call_kwargs
        assert isinstance(call_kwargs["callbacks"], MCPContextProgressCallback)

    async def test_get_person_profile_passes_max_scrolls(
        self, mock_context, serve_extractor
    ):
        """Verify max_scrolls parameter is forwarded to scrape_person."""
        expected = {
            "url": "https://www.linkedin.com/in/test-user/",
            "sections": {"main_profile": "John Doe"},
        }
        mock_extractor = _make_mock_extractor(expected)

        from linkedin_mcp_server.tools.person import register_person_tools

        mcp = FastMCP("test")
        register_person_tools(mcp)

        serve_extractor(mock_extractor)
        tool_fn = await get_tool_fn(mcp, "get_person_profile")
        await tool_fn(
            "test-user",
            mock_context,
            max_scrolls=15,
        )

        call_kwargs = mock_extractor.scrape_person.call_args.kwargs
        assert call_kwargs["max_scrolls"] == 15

    async def test_get_person_profile_rejects_invalid_max_scrolls(self, mock_context):
        """Verify max_scrolls=0 is rejected by Field(ge=1) validation."""
        # FastMCP wraps the pydantic error raised by Field() constraints in
        # its own ValidationError, which does not subclass pydantic's.
        from fastmcp.exceptions import ValidationError

        from linkedin_mcp_server.tools.person import register_person_tools

        mcp = FastMCP("test")
        register_person_tools(mcp)

        with pytest.raises(ValidationError, match="max_scrolls"):
            await mcp.call_tool(
                "get_person_profile",
                {"linkedin_username": "test-user", "max_scrolls": 0},
            )

    async def test_get_person_profile_unknown_section(
        self, mock_context, serve_extractor
    ):
        expected = {
            "url": "https://www.linkedin.com/in/test-user/",
            "sections": {"main_profile": "John Doe"},
        }
        mock_extractor = _make_mock_extractor(expected)

        from linkedin_mcp_server.tools.person import register_person_tools

        mcp = FastMCP("test")
        register_person_tools(mcp)

        serve_extractor(mock_extractor)
        tool_fn = await get_tool_fn(mcp, "get_person_profile")
        result = await tool_fn(
            "test-user",
            mock_context,
            sections="bogus_section",
        )
        assert result["unknown_sections"] == ["bogus_section"]

    async def test_get_person_profile_error(self, mock_context, serve_extractor):
        from fastmcp.exceptions import ToolError

        from linkedin_mcp_server.exceptions import SessionExpiredError

        mock_extractor = MagicMock()
        mock_extractor.scrape_person = AsyncMock(side_effect=SessionExpiredError())

        from linkedin_mcp_server.tools.person import register_person_tools

        mcp = FastMCP("test")
        register_person_tools(mcp)

        serve_extractor(mock_extractor)
        tool_fn = await get_tool_fn(mcp, "get_person_profile")
        with pytest.raises(ToolError, match="Session expired"):
            await tool_fn("test-user", mock_context)

    async def test_get_person_profile_auth_error(self, monkeypatch):
        """Auth failures in the DI layer trigger auto-relogin and report the login browser."""
        from fastmcp.exceptions import ToolError

        from linkedin_mcp_server.core.exceptions import AuthenticationError
        from linkedin_mcp_server.exceptions import AuthenticationStartedError

        mock_browser = MagicMock()
        mock_browser.page = MagicMock()
        monkeypatch.setattr(
            "linkedin_mcp_server.dependencies.ensure_tool_ready_or_raise",
            AsyncMock(return_value=None),
        )
        monkeypatch.setattr(
            "linkedin_mcp_server.dependencies.get_or_create_browser",
            AsyncMock(return_value=mock_browser),
        )
        monkeypatch.setattr(
            "linkedin_mcp_server.dependencies.ensure_authenticated",
            AsyncMock(side_effect=AuthenticationError("Session expired or invalid.")),
        )
        monkeypatch.setattr(
            "linkedin_mcp_server.dependencies.get_runtime_policy",
            lambda: "managed",
        )
        monkeypatch.setattr(
            "linkedin_mcp_server.dependencies.close_browser",
            AsyncMock(return_value=None),
        )
        monkeypatch.setattr(
            "linkedin_mcp_server.dependencies.invalidate_auth_and_trigger_relogin",
            AsyncMock(
                side_effect=AuthenticationStartedError(
                    "Session expired. A login browser window has been opened."
                )
            ),
        )

        from linkedin_mcp_server.tools.person import register_person_tools

        mcp = FastMCP("test")
        register_person_tools(mcp)

        with pytest.raises(ToolError, match="Session expired"):
            await mcp.call_tool("get_person_profile", {"linkedin_username": "test"})

    async def test_search_people(self, mock_context, serve_extractor):
        expected = {
            "url": "https://www.linkedin.com/search/results/people/?keywords=AI+engineer&location=New+York",
            "sections": {"search_results": "Jane Doe\nAI Engineer at Acme\nNew York"},
        }
        mock_extractor = _make_mock_extractor(expected)

        from linkedin_mcp_server.tools.person import register_person_tools

        mcp = FastMCP("test")
        register_person_tools(mcp)

        serve_extractor(mock_extractor)
        tool_fn = await get_tool_fn(mcp, "search_people")
        result = await tool_fn("AI engineer", mock_context, location="New York")
        assert "search_results" in result["sections"]
        assert "pages_visited" not in result
        mock_extractor.search_people.assert_awaited_once_with(
            "AI engineer",
            "New York",
            network=None,
            current_company=None,
        )

    async def test_search_people_with_network_and_company_filters(
        self, mock_context, serve_extractor
    ):
        expected = {
            "url": (
                "https://www.linkedin.com/search/results/people/"
                "?keywords=engineer&network=%5B%22F%22%5D"
                "&currentCompany=%5B%221115%22%5D"
            ),
            "sections": {
                "search_results": "Jennifer Bonuso\nPresident Americas at SAP"
            },
        }
        mock_extractor = _make_mock_extractor(expected)

        from linkedin_mcp_server.tools.person import register_person_tools

        mcp = FastMCP("test")
        register_person_tools(mcp)

        serve_extractor(mock_extractor)
        tool_fn = await get_tool_fn(mcp, "search_people")
        result = await tool_fn(
            "engineer",
            mock_context,
            network=["F"],
            current_company="1115",
        )
        assert "search_results" in result["sections"]
        mock_extractor.search_people.assert_awaited_once_with(
            "engineer",
            None,
            network=["F"],
            current_company="1115",
        )

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("F", ["F"]),
            ('["F"]', ["F"]),
            ('["F", "S"]', ["F", "S"]),
            ("F,S", ["F", "S"]),
            (" F , S ", ["F", "S"]),
            ("", []),
            (["F"], ["F"]),
            (None, None),
        ],
    )
    def test_coerce_str_list_repairs_stringified_arrays(self, raw, expected):
        """A client that flattens the array must still produce a list.

        Lists and None pass through untouched, so a well-behaved client is
        unaffected.
        """
        from linkedin_mcp_server.tools.person import _coerce_str_list

        assert _coerce_str_list(raw) == expected

    async def test_search_people_accepts_stringified_network(self, monkeypatch):
        """Regression for #739.

        The published schema for ``network`` is ``anyOf: [array, null]``, but
        clients that collapse that union send a bare string. Going through
        ``call_tool`` exercises the pydantic validation the direct-``fn`` tests
        skip, which is where the original failure lived.
        """
        import linkedin_mcp_server.tools.person as person_module
        from linkedin_mcp_server.tools.person import register_person_tools

        expected = {
            "url": (
                "https://www.linkedin.com/search/results/people/"
                "?keywords=engineer&network=%5B%22F%22%5D"
            ),
            "sections": {"search_results": "Jane Doe"},
        }
        mock_extractor = _make_mock_extractor(expected)

        async def _fake_get_ready_extractor(ctx, tool_name):
            return mock_extractor

        monkeypatch.setattr(
            person_module, "get_ready_extractor", _fake_get_ready_extractor
        )

        mcp = FastMCP("test")
        register_person_tools(mcp)

        await mcp.call_tool("search_people", {"keywords": "engineer", "network": "F"})

        mock_extractor.search_people.assert_awaited_once_with(
            "engineer",
            None,
            network=["F"],
            current_company=None,
        )

    async def test_search_people_validation_error_surfaced_as_tool_error(
        self, mock_context, serve_extractor
    ):
        """A FilterValidationError raised by the extractor should surface to
        the MCP client as a ToolError carrying the same message, rather than
        being collapsed to the generic "Error calling tool" mask."""
        from fastmcp.exceptions import ToolError

        from linkedin_mcp_server.scraping.contracts import FilterValidationError
        from linkedin_mcp_server.tools.person import register_person_tools

        mock_extractor = MagicMock()
        mock_extractor.search_people = AsyncMock(
            side_effect=FilterValidationError("must be a numeric URN")
        )

        mcp = FastMCP("test")
        register_person_tools(mcp)
        serve_extractor(mock_extractor)
        tool_fn = await get_tool_fn(mcp, "search_people")

        with pytest.raises(ToolError, match="must be a numeric URN"):
            await tool_fn(
                "engineer",
                mock_context,
                current_company="1115",
            )

    async def test_connect_with_person(self, mock_context, serve_extractor):
        expected = {
            "url": "https://www.linkedin.com/in/test-user/",
            "status": "connected",
            "message": "Connection request sent.",
            "note_sent": True,
        }
        mock_extractor = _make_mock_extractor(expected)

        from linkedin_mcp_server.tools.person import register_person_tools

        mcp = FastMCP("test")
        register_person_tools(mcp)

        serve_extractor(mock_extractor)
        tool_fn = await get_tool_fn(mcp, "connect_with_person")
        result = await tool_fn(
            "https://www.linkedin.com/in/test-user/",
            mock_context,
            note="Let us connect.",
        )

        assert result["status"] == "connected"
        assert result["note_sent"] is True
        mock_extractor.connect_with_person.assert_awaited_once_with(
            "test-user",
            note="Let us connect.",
        )

    async def test_connect_with_person_no_note(self, mock_context, serve_extractor):
        expected = {
            "url": "https://www.linkedin.com/in/test-user/",
            "status": "connected",
            "message": "Connection request sent.",
            "note_sent": False,
        }
        mock_extractor = _make_mock_extractor(expected)

        from linkedin_mcp_server.tools.person import register_person_tools

        mcp = FastMCP("test")
        register_person_tools(mcp)

        serve_extractor(mock_extractor)
        tool_fn = await get_tool_fn(mcp, "connect_with_person")
        result = await tool_fn(
            "test-user",
            mock_context,
        )

        assert result["status"] == "connected"
        mock_extractor.connect_with_person.assert_awaited_once_with(
            "test-user",
            note=None,
        )

    async def test_connect_with_person_custom_note_limit_reached(
        self, mock_context, serve_extractor
    ):
        """The custom_note_limit_reached status returns LinkedIn's message."""
        expected = {
            "url": "https://www.linkedin.com/in/test-user/",
            "status": "custom_note_limit_reached",
            "message": "Wysyłaj nieograniczoną liczbę spersonalizowanych zaproszeń dzięki Premium",
            "note_sent": False,
        }
        mock_extractor = _make_mock_extractor(expected)

        from linkedin_mcp_server.tools.person import register_person_tools

        mcp = FastMCP("test")
        register_person_tools(mcp)

        serve_extractor(mock_extractor)
        tool_fn = await get_tool_fn(mcp, "connect_with_person")
        result = await tool_fn(
            "test-user",
            mock_context,
            note="Hello!",
        )

        assert result["status"] == "custom_note_limit_reached"
        assert (
            result["message"]
            == "Wysyłaj nieograniczoną liczbę spersonalizowanych zaproszeń dzięki Premium"
        )
        assert result["note_sent"] is False
        mock_extractor.connect_with_person.assert_awaited_once_with(
            "test-user",
            note="Hello!",
        )

    async def test_connect_with_person_auth_error(self, monkeypatch):
        """Auth failures in the DI layer trigger auto-relogin and report the login browser."""
        from fastmcp.exceptions import ToolError

        from linkedin_mcp_server.core.exceptions import AuthenticationError
        from linkedin_mcp_server.exceptions import AuthenticationStartedError

        mock_browser = MagicMock()
        mock_browser.page = MagicMock()
        monkeypatch.setattr(
            "linkedin_mcp_server.dependencies.ensure_tool_ready_or_raise",
            AsyncMock(return_value=None),
        )
        monkeypatch.setattr(
            "linkedin_mcp_server.dependencies.get_or_create_browser",
            AsyncMock(return_value=mock_browser),
        )
        monkeypatch.setattr(
            "linkedin_mcp_server.dependencies.ensure_authenticated",
            AsyncMock(side_effect=AuthenticationError("Session expired or invalid.")),
        )
        monkeypatch.setattr(
            "linkedin_mcp_server.dependencies.get_runtime_policy",
            lambda: "managed",
        )
        monkeypatch.setattr(
            "linkedin_mcp_server.dependencies.close_browser",
            AsyncMock(return_value=None),
        )
        monkeypatch.setattr(
            "linkedin_mcp_server.dependencies.invalidate_auth_and_trigger_relogin",
            AsyncMock(
                side_effect=AuthenticationStartedError(
                    "Session expired. A login browser window has been opened."
                )
            ),
        )

        from linkedin_mcp_server.tools.person import register_person_tools

        mcp = FastMCP("test")
        register_person_tools(mcp)

        with pytest.raises(ToolError, match="Session expired"):
            await mcp.call_tool(
                "connect_with_person",
                {"linkedin_username": "test"},
            )


class TestCompanyTools:
    async def test_get_company_profile(self, mock_context, serve_extractor):
        expected = {
            "url": "https://www.linkedin.com/company/testcorp/",
            "sections": {"about": "TestCorp\nWe build things"},
        }
        mock_extractor = _make_mock_extractor(expected)

        from linkedin_mcp_server.tools.company import register_company_tools

        mcp = FastMCP("test")
        register_company_tools(mcp)

        serve_extractor(mock_extractor)
        tool_fn = await get_tool_fn(mcp, "get_company_profile")
        result = await tool_fn(
            "https://uk.linkedin.com/company/testcorp/",
            mock_context,
        )
        assert "about" in result["sections"]
        assert "pages_visited" not in result
        assert (
            mock_extractor.scrape_company.await_args.args[0]
            == "https://uk.linkedin.com/company/testcorp/"
        )

    @pytest.mark.parametrize(
        "tool_name", ["get_company_profile", "get_company_employees"]
    )
    @pytest.mark.parametrize("slug", ["linkedin.com", "lnkd.in"])
    async def test_company_collision_slug_reaches_scraper(
        self, mock_context, serve_extractor, tool_name, slug
    ):
        from linkedin_mcp_server.scraping.company import CompanyScraper
        from linkedin_mcp_server.tools.company import register_company_tools

        capture = MagicMock()
        capture.capture = AsyncMock(
            return_value=ExtractedSection(text="company text", references=[])
        )
        scraper = CompanyScraper(MagicMock(), capture)
        mcp = FastMCP("test")
        register_company_tools(mcp)

        serve_extractor(scraper)
        tool_fn = await get_tool_fn(mcp, tool_name)
        result = await tool_fn(f"/company/{slug}/", mock_context)

        suffix = "/" if tool_name == "get_company_profile" else "/people/"
        url = f"https://www.linkedin.com/company/{slug}{suffix}"
        assert result["url"] == url
        assert result["sections"]
        capture.capture.assert_awaited_once()
        assert capture.capture.await_args is not None
        capture_suffix = "/about/" if tool_name == "get_company_profile" else suffix
        assert capture.capture.await_args.args[0] == (
            f"https://www.linkedin.com/company/{slug}{capture_suffix}"
        )

    async def test_get_company_posts_normalizes_a_pasted_link(
        self, mock_context, serve_extractor
    ):
        """get_company_posts builds its URL in the tool, not in the extractor.

        That makes it the one wiring point the extractor tests cannot reach, and
        the only place a pasted company link would still become
        /company/https://de.linkedin.com/company/testcorp/posts/.
        """
        mock_extractor = _make_mock_extractor({})

        from linkedin_mcp_server.tools.company import register_company_tools

        mcp = FastMCP("test")
        register_company_tools(mcp)

        serve_extractor(mock_extractor)
        tool_fn = await get_tool_fn(mcp, "get_company_posts")
        result = await tool_fn(
            "https://de.linkedin.com/company/testcorp/",
            mock_context,
        )
        assert result["url"] == "https://www.linkedin.com/company/testcorp/posts/"
        assert (
            mock_extractor.extract_page.call_args.args[0]
            == "https://www.linkedin.com/company/testcorp/posts/"
        )

    async def test_get_company_posts_refuses_a_traversal_value(
        self, mock_context, serve_extractor
    ):
        mock_extractor = _make_mock_extractor({})

        from linkedin_mcp_server.tools.company import register_company_tools

        mcp = FastMCP("test")
        register_company_tools(mcp)

        serve_extractor(mock_extractor)
        tool_fn = await get_tool_fn(mcp, "get_company_posts")
        with pytest.raises(Exception):
            await tool_fn("../../feed", mock_context)
        mock_extractor.extract_page.assert_not_called()

    async def test_get_company_profile_passes_callbacks(
        self, mock_context, serve_extractor
    ):
        """Verify tool wires MCPContextProgressCallback to the extractor."""
        expected = {
            "url": "https://www.linkedin.com/company/testcorp/",
            "sections": {"about": "TestCorp\nWe build things"},
        }
        mock_extractor = _make_mock_extractor(expected)

        from linkedin_mcp_server.tools.company import register_company_tools

        mcp = FastMCP("test")
        register_company_tools(mcp)

        serve_extractor(mock_extractor)
        tool_fn = await get_tool_fn(mcp, "get_company_profile")
        await tool_fn("testcorp", mock_context)

        call_kwargs = mock_extractor.scrape_company.call_args.kwargs
        assert "callbacks" in call_kwargs
        assert isinstance(call_kwargs["callbacks"], MCPContextProgressCallback)

    async def test_get_company_profile_unknown_section(
        self, mock_context, serve_extractor
    ):
        expected = {
            "url": "https://www.linkedin.com/company/testcorp/",
            "sections": {"about": "TestCorp\nWe build things"},
        }
        mock_extractor = _make_mock_extractor(expected)

        from linkedin_mcp_server.tools.company import register_company_tools

        mcp = FastMCP("test")
        register_company_tools(mcp)

        serve_extractor(mock_extractor)
        tool_fn = await get_tool_fn(mcp, "get_company_profile")
        result = await tool_fn("testcorp", mock_context, sections="bogus")
        assert result["unknown_sections"] == ["bogus"]

    async def test_get_company_posts(self, mock_context, serve_extractor):
        mock_extractor = MagicMock()
        mock_extractor.extract_page = AsyncMock(
            return_value=ExtractedSection(text="Post 1\nPost 2", references=[])
        )

        from linkedin_mcp_server.tools.company import register_company_tools

        mcp = FastMCP("test")
        register_company_tools(mcp)

        serve_extractor(mock_extractor)
        tool_fn = await get_tool_fn(mcp, "get_company_posts")
        result = await tool_fn("testcorp", mock_context)
        assert "posts" in result["sections"]
        assert result["sections"]["posts"] == "Post 1\nPost 2"
        assert "pages_visited" not in result
        assert "sections_requested" not in result

    async def test_get_company_posts_omits_rate_limited_sentinel(
        self, mock_context, serve_extractor
    ):
        mock_extractor = MagicMock()
        mock_extractor.extract_page = AsyncMock(
            return_value=ExtractedSection(text=RATE_LIMITED_SECTION_TEXT, references=[])
        )

        from linkedin_mcp_server.tools.company import register_company_tools

        mcp = FastMCP("test")
        register_company_tools(mcp)

        serve_extractor(mock_extractor)
        tool_fn = await get_tool_fn(mcp, "get_company_posts")
        result = await tool_fn("testcorp", mock_context)
        assert result["sections"] == {}
        assert result["section_errors"]["posts"]["error_type"] == "rate_limit"

    async def test_get_company_posts_returns_section_errors(
        self, mock_context, serve_extractor
    ):
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

        serve_extractor(mock_extractor)
        tool_fn = await get_tool_fn(mcp, "get_company_posts")
        result = await tool_fn("testcorp", mock_context)
        assert result["sections"] == {}
        assert result["section_errors"]["posts"]["issue_template_path"] == (
            "/tmp/company-posts-issue.md"
        )

    async def test_get_company_posts_omits_orphaned_references(
        self, mock_context, serve_extractor
    ):
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

        serve_extractor(mock_extractor)
        tool_fn = await get_tool_fn(mcp, "get_company_posts")
        result = await tool_fn("testcorp", mock_context)
        assert result["sections"] == {}
        assert "references" not in result


class TestJobTools:
    async def test_get_job_details(self, mock_context, serve_extractor):
        expected = {
            "url": "https://www.linkedin.com/jobs/view/12345/",
            "sections": {"job_posting": "Software Engineer\nGreat opportunity"},
        }
        mock_extractor = _make_mock_extractor(expected)

        from linkedin_mcp_server.tools.job import register_job_tools

        mcp = FastMCP("test")
        register_job_tools(mcp)

        serve_extractor(mock_extractor)
        tool_fn = await get_tool_fn(mcp, "get_job_details")
        result = await tool_fn(
            "https://www.linkedin.com/jobs/view/12345/",
            mock_context,
        )
        assert "job_posting" in result["sections"]
        assert "pages_visited" not in result
        mock_extractor.scrape_job.assert_awaited_once_with("12345")

    async def test_search_jobs(self, mock_context, serve_extractor):
        expected = {
            "url": "https://www.linkedin.com/jobs/search/?keywords=python",
            "sections": {"search_results": "Job 1\nJob 2"},
        }
        mock_extractor = _make_mock_extractor(expected)

        from linkedin_mcp_server.tools.job import register_job_tools

        mcp = FastMCP("test")
        register_job_tools(mcp)

        serve_extractor(mock_extractor)
        tool_fn = await get_tool_fn(mcp, "search_jobs")
        result = await tool_fn("python", mock_context, location="Remote")
        assert "search_results" in result["sections"]
        assert "pages_visited" not in result

    async def test_search_jobs_is_bounded_by_the_registered_timeout(
        self, mock_context, serve_extractor
    ):
        """The loop stops itself by the same figure FastMCP cancels on.

        Registered with a non-default timeout, because dropping the argument
        leaves the extractor budgeting against its own 180s default while
        FastMCP still cancels at 60s. The search would then be killed mid-page
        and every page already gathered thrown away, which is the loss the
        bound exists to prevent.

        What arrives is what is left of it: FastMCP starts its clock before
        this tool runs, and acquiring the browser is spent from the same 60s.
        A cold start of three seconds passed on as a full sixty is the same
        defect one layer down.
        """
        mock_extractor = _make_mock_extractor(
            {
                "url": "https://www.linkedin.com/jobs/search/?keywords=python",
                "sections": {"search_results": "Job 1"},
            }
        )

        from linkedin_mcp_server.tools.job import register_job_tools

        mcp = FastMCP("test")
        register_job_tools(mcp, tool_timeout=60.0)

        serve_extractor(mock_extractor)
        tool_fn = await get_tool_fn(mcp, "search_jobs")
        await tool_fn("python", mock_context)

        passed = mock_extractor.search_jobs.await_args.kwargs["tool_timeout"]
        assert passed <= 60.0
        assert passed == pytest.approx(60.0, abs=1.0)

    async def test_the_search_budget_pays_for_the_browser_it_waited_on(
        self, mock_context
    ):
        """FastMCP starts its clock before this tool runs.

        Acquiring the browser is spent from the same figure, so passing it on
        whole leaves the extractor planning against time it no longer has. A
        cold start is where this bites: warm, the wait is nothing and the
        budget is the full timeout either way, which is why asserting the
        figure alone cannot see the defect.
        """
        mock_extractor = _make_mock_extractor(
            {
                "url": "https://www.linkedin.com/jobs/search/?keywords=python",
                "sections": {"search_results": "Job 1"},
            }
        )

        async def slow_start(*args, **kwargs):
            await asyncio.sleep(0.3)
            return mock_extractor

        from linkedin_mcp_server.tools.job import register_job_tools

        mcp = FastMCP("test")
        register_job_tools(mcp, tool_timeout=60.0)

        tool_fn = await get_tool_fn(mcp, "search_jobs")
        with patch(
            "linkedin_mcp_server.tools.job.get_ready_extractor",
            side_effect=slow_start,
        ):
            await tool_fn("python", mock_context)

        passed = mock_extractor.search_jobs.await_args.kwargs["tool_timeout"]
        assert passed < 59.9

    async def test_get_saved_jobs(self, mock_context, serve_extractor):
        expected = {
            "url": "https://www.linkedin.com/my-items/saved-jobs/",
            "sections": {"saved_jobs": "Saved Job 1\nSaved Job 2"},
            "job_ids": ["111", "222"],
        }
        mock_extractor = _make_mock_extractor(expected)

        from linkedin_mcp_server.tools.job import register_job_tools

        mcp = FastMCP("test")
        register_job_tools(mcp)

        serve_extractor(mock_extractor)
        tool_fn = await get_tool_fn(mcp, "get_saved_jobs")
        result = await tool_fn(mock_context, max_pages=2)
        assert "saved_jobs" in result["sections"]
        assert result["job_ids"] == ["111", "222"]
        mock_extractor.get_saved_jobs.assert_awaited_once_with(max_pages=2)


class TestGetSidebarProfilesTool:
    async def test_get_sidebar_profiles_success(self, mock_context, serve_extractor):
        expected = {
            "url": "https://www.linkedin.com/in/test-user/",
            "sidebar_profiles": {
                "more_profiles_for_you": ["/in/alice/", "/in/bob/"],
            },
        }
        mock_extractor = _make_mock_extractor(expected)

        from linkedin_mcp_server.tools.person import register_person_tools

        mcp = FastMCP("test")
        register_person_tools(mcp)

        serve_extractor(mock_extractor)
        tool_fn = await get_tool_fn(mcp, "get_sidebar_profiles")
        result = await tool_fn(
            "https://www.linkedin.com/in/test-user/",
            mock_context,
        )

        assert result["url"] == "https://www.linkedin.com/in/test-user/"
        assert "more_profiles_for_you" in result["sidebar_profiles"]
        mock_extractor.get_sidebar_profiles.assert_awaited_once_with("test-user")

    async def test_get_sidebar_profiles_empty_result(
        self, mock_context, serve_extractor
    ):
        expected = {
            "url": "https://www.linkedin.com/in/test-user/",
            "sidebar_profiles": {},
        }
        mock_extractor = _make_mock_extractor(expected)

        from linkedin_mcp_server.tools.person import register_person_tools

        mcp = FastMCP("test")
        register_person_tools(mcp)

        serve_extractor(mock_extractor)
        tool_fn = await get_tool_fn(mcp, "get_sidebar_profiles")
        result = await tool_fn("test-user", mock_context)

        assert result["sidebar_profiles"] == {}

    async def test_get_sidebar_profiles_error(self, mock_context, serve_extractor):
        from fastmcp.exceptions import ToolError

        from linkedin_mcp_server.exceptions import SessionExpiredError

        mock_extractor = MagicMock()
        mock_extractor.get_sidebar_profiles = AsyncMock(
            side_effect=SessionExpiredError()
        )

        from linkedin_mcp_server.tools.person import register_person_tools

        mcp = FastMCP("test")
        register_person_tools(mcp)

        serve_extractor(mock_extractor)
        tool_fn = await get_tool_fn(mcp, "get_sidebar_profiles")
        with pytest.raises(ToolError, match="Session expired"):
            await tool_fn("test-user", mock_context)


class TestMessagingTools:
    async def test_get_inbox_success(self, mock_context, serve_extractor):
        expected = {
            "url": "https://www.linkedin.com/messaging/",
            "sections": {"inbox": "Conversation 1\nConversation 2"},
        }
        mock_extractor = _make_mock_extractor(expected)

        from linkedin_mcp_server.tools.messaging import register_messaging_tools

        mcp = FastMCP("test")
        register_messaging_tools(mcp)

        serve_extractor(mock_extractor)
        tool_fn = await get_tool_fn(mcp, "get_inbox")
        result = await tool_fn(mock_context)

        assert result["sections"]["inbox"] == "Conversation 1\nConversation 2"
        mock_extractor.get_inbox.assert_awaited_once_with(limit=20)

    async def test_get_conversation_success(self, mock_context, serve_extractor):
        expected = {
            "url": "https://www.linkedin.com/messaging/thread/abc123/",
            "sections": {"conversation": "Hello!\nHi there!"},
        }
        mock_extractor = _make_mock_extractor(expected)

        from linkedin_mcp_server.tools.messaging import register_messaging_tools

        mcp = FastMCP("test")
        register_messaging_tools(mcp)

        serve_extractor(mock_extractor)
        tool_fn = await get_tool_fn(mcp, "get_conversation")
        result = await tool_fn(
            mock_context,
            linkedin_username="https://www.linkedin.com/in/testuser/",
        )

        assert result["sections"]["conversation"] == "Hello!\nHi there!"
        mock_extractor.get_conversation.assert_awaited_once_with(
            linkedin_username="testuser", thread_id=None, index=0
        )

    async def test_get_conversation_normalizes_a_thread_url(
        self, mock_context, serve_extractor
    ):
        mock_extractor = _make_mock_extractor(
            {
                "url": "https://www.linkedin.com/messaging/thread/abc123/",
                "sections": {"conversation": "Hello!"},
            }
        )

        from linkedin_mcp_server.tools.messaging import register_messaging_tools

        mcp = FastMCP("test")
        register_messaging_tools(mcp)

        serve_extractor(mock_extractor)
        tool_fn = await get_tool_fn(mcp, "get_conversation")
        await tool_fn(
            mock_context,
            thread_id="https://www.linkedin.com/messaging/thread/abc123/",
        )

        mock_extractor.get_conversation.assert_awaited_once_with(
            linkedin_username=None, thread_id="abc123", index=0
        )

    async def test_get_conversation_thread_id_takes_precedence_over_username(self):
        from linkedin_mcp_server.tools.messaging import register_messaging_tools

        mock_extractor = _make_mock_extractor(
            {"url": "https://www.linkedin.com/messaging/thread/abc123/", "sections": {}}
        )
        mcp = FastMCP("test")
        register_messaging_tools(mcp)
        ready = AsyncMock(return_value=mock_extractor)

        with patch("linkedin_mcp_server.tools.messaging.get_ready_extractor", ready):
            await mcp.call_tool(
                "get_conversation",
                {
                    "linkedin_username": "/feed/",
                    "thread_id": "https://www.linkedin.com/messaging/thread/abc123/",
                    "index": 2,
                },
            )

        ready.assert_awaited_once()
        mock_extractor.get_conversation.assert_awaited_once_with(
            linkedin_username="/feed/", thread_id="abc123", index=2
        )

    async def test_get_conversation_rejects_missing_identifier_before_extractor(self):
        from fastmcp.exceptions import ToolError

        from linkedin_mcp_server.tools.messaging import register_messaging_tools

        mcp = FastMCP("test")
        register_messaging_tools(mcp)
        ready = AsyncMock(side_effect=AssertionError("get_ready_extractor was called"))

        with patch("linkedin_mcp_server.tools.messaging.get_ready_extractor", ready):
            with pytest.raises(ToolError) as raised:
                await mcp.call_tool("get_conversation", {})

        assert str(raised.value) == (
            "Provide at least one of linkedin_username or thread_id"
        )
        ready.assert_not_awaited()

    async def test_search_conversations_success(self, mock_context, serve_extractor):
        expected = {
            "url": "https://www.linkedin.com/messaging/",
            "sections": {"search_results": "Result 1\nResult 2"},
        }
        mock_extractor = _make_mock_extractor(expected)

        from linkedin_mcp_server.tools.messaging import register_messaging_tools

        mcp = FastMCP("test")
        register_messaging_tools(mcp)

        serve_extractor(mock_extractor)
        tool_fn = await get_tool_fn(mcp, "search_conversations")
        result = await tool_fn("hello", mock_context)

        assert result["sections"]["search_results"] == "Result 1\nResult 2"
        mock_extractor.search_conversations.assert_awaited_once_with("hello", limit=20)

    @pytest.mark.parametrize(
        ("tool", "section"),
        [("get_inbox", "inbox"), ("search_conversations", "search_results")],
    )
    async def test_listing_section_errors_pass_through_unchanged(
        self, mock_context, serve_extractor, tool, section
    ):
        expected = {
            "url": "https://www.linkedin.com/messaging/",
            "sections": {section: "Ada Lovelace"},
            "references": {
                section: [
                    {
                        "kind": "conversation",
                        "url": "/messaging/thread/2-ada/",
                        "context": section,
                        "text": "Ada Lovelace",
                    }
                ]
            },
            "section_errors": {
                section: {
                    "error_type": "thread_attribution_stopped",
                    "error_message": "Click-derived conversation references stop.",
                }
            },
        }
        mock_extractor = _make_mock_extractor(expected)

        from linkedin_mcp_server.tools.messaging import register_messaging_tools

        mcp = FastMCP("test")
        register_messaging_tools(mcp)

        serve_extractor(mock_extractor)
        tool_fn = await get_tool_fn(mcp, tool)
        if tool == "get_inbox":
            result = await tool_fn(mock_context)
        else:
            result = await tool_fn("ada", mock_context)

        assert result == expected

    @staticmethod
    def _diagnostics_marker(monkeypatch):
        """Stand in for the issue-report builder so no artifact is written."""
        monkeypatch.setattr(
            "linkedin_mcp_server.error_handler.build_issue_diagnostics",
            lambda *args, **kwargs: {"marker": True},
        )
        monkeypatch.setattr(
            "linkedin_mcp_server.error_handler.format_tool_error_with_diagnostics",
            lambda message, diagnostics: f"{message}\n[issue diagnostics]",
        )

    async def test_a_scraper_error_from_get_conversation_keeps_diagnostics(
        self, monkeypatch
    ):
        """Mapper control: the base scraper error takes the diagnostics path."""
        from fastmcp.exceptions import ToolError

        from linkedin_mcp_server.core.exceptions import LinkedInScraperException
        from linkedin_mcp_server.tools.messaging import register_messaging_tools

        self._diagnostics_marker(monkeypatch)
        mock_extractor = _make_mock_extractor({})
        mock_extractor.get_conversation = AsyncMock(
            side_effect=LinkedInScraperException("Could not verify index 0.")
        )
        mcp = FastMCP("test")
        register_messaging_tools(mcp)

        with patch(
            "linkedin_mcp_server.tools.messaging.get_ready_extractor",
            AsyncMock(return_value=mock_extractor),
        ):
            with pytest.raises(ToolError) as raised:
                await mcp.call_tool("get_conversation", {"linkedin_username": "jacki"})

        assert str(raised.value) == "Could not verify index 0.\n[issue diagnostics]"

    async def test_an_unverifiable_username_index_is_refused_with_diagnostics(
        self, monkeypatch, mock_page
    ):
        """The real owner's refusal, through the registered tool and mapper.

        Only the page boundaries and the scan outcome are scripted: the first
        matching row's click did not open a different thread. The refusal must
        name the reason and keep the issue diagnostics, which it loses if the
        owner raises it as a caller-reference error.

        The tool receives the conversation owner wired the way the facade wires
        it; the facade's own delegation is covered by the facade contract tests
        and this module may not import the facade.
        """
        from fastmcp.exceptions import ToolError

        from linkedin_mcp_server.scraping.content import PageContentReader
        from linkedin_mcp_server.scraping.conversations import (
            ConversationReader,
            _StoppedRow,
            _ThreadRefScan,
        )
        from linkedin_mcp_server.scraping.navigation import PageNavigator
        from linkedin_mcp_server.scraping.profile_page import ProfilePageReader
        from linkedin_mcp_server.scraping.session import ScrapingSession
        from linkedin_mcp_server.tools.messaging import register_messaging_tools

        async def no_message_target() -> Any:
            raise AssertionError("the conversation owner reads no message target")

        self._diagnostics_marker(monkeypatch)
        session = ScrapingSession(mock_page)
        extractor = ConversationReader(
            session,
            PageNavigator(session),
            PageContentReader(session),
            ProfilePageReader(session, no_message_target),
        )
        mcp = FastMCP("test")
        register_messaging_tools(mcp)
        stopped = _ThreadRefScan(
            refs=[], stopped_at=_StoppedRow("Select conversation with Jacki", 0)
        )
        root = AsyncMock()
        navigate = AsyncMock()

        with (
            patch(
                "linkedin_mcp_server.tools.messaging.get_ready_extractor",
                AsyncMock(return_value=extractor),
            ),
            patch.object(ScrapingSession, "check_rate_limit", new_callable=AsyncMock),
            patch.object(ScrapingSession, "dismiss_modal", new_callable=AsyncMock),
            patch.object(PageNavigator, "_navigate_to_page", navigate),
            patch.object(
                ProfilePageReader,
                "_read_profile_display_name",
                new_callable=AsyncMock,
                return_value="Jacki",
            ),
            patch.object(
                ConversationReader,
                "_extract_conversation_thread_refs",
                new_callable=AsyncMock,
                return_value=stopped,
            ),
            patch.object(PageContentReader, "_extract_root_content", root),
        ):
            with pytest.raises(ToolError) as raised:
                await mcp.call_tool("get_conversation", {"linkedin_username": "jacki"})

        assert str(raised.value) == (
            "Could not verify conversation index 0 for jacki: 0 conversation(s) "
            "were verified before a matching row that did not open a different "
            "thread within the poll budget. It may already be open, or the click "
            "may not have navigated in time. Pass a known thread_id instead."
            "\n[issue diagnostics]"
        )
        assert [call.args[0] for call in navigate.await_args_list] == [
            "https://www.linkedin.com/in/jacki/",
            "https://www.linkedin.com/messaging/compose/",
        ]
        root.assert_not_awaited()

    async def test_send_message_success(self, mock_context, serve_extractor):
        expected = {
            "url": "https://www.linkedin.com/messaging/thread/abc123/",
            "status": "sent",
            "message": "Message sent.",
            "recipient_selected": True,
            "sent": True,
        }
        mock_extractor = _make_mock_extractor(expected)

        from linkedin_mcp_server.tools.messaging import register_messaging_tools

        mcp = FastMCP("test")
        register_messaging_tools(mcp)

        serve_extractor(mock_extractor)
        tool_fn = await get_tool_fn(mcp, "send_message")
        result = await tool_fn(
            "https://www.linkedin.com/in/testuser/",
            "Hello!",
            True,
            mock_context,
        )

        assert result["status"] == "sent"
        assert result["sent"] is True
        mock_extractor.send_message.assert_awaited_once_with(
            "testuser", "Hello!", confirm_send=True, profile_urn=None
        )

    async def test_send_message_description_explains_connection_handoff(self):
        from linkedin_mcp_server.tools.messaging import register_messaging_tools

        mcp = FastMCP("test")
        register_messaging_tools(mcp)

        tool = await mcp.get_tool("send_message")
        assert tool is not None
        assert tool.description is not None
        description = " ".join(tool.description.split())
        assert (
            "If LinkedIn does not expose a normal Message action, use "
            "connect_with_person first, then retry send_message only after the "
            "connection request is accepted."
        ) in description

    async def test_send_message_schema_explains_single_line_controls(self):
        from linkedin_mcp_server.tools.messaging import register_messaging_tools

        mcp = FastMCP("test")
        register_messaging_tools(mcp)

        tool = await mcp.get_tool("send_message")
        assert tool is not None
        message_schema = tool.parameters["properties"]["message"]
        assert " ".join(message_schema["description"].split()) == (
            "Single-line message text to send. C0 control characters and DEL are "
            "rejected, including CR, LF, and tab."
        )

    @pytest.mark.parametrize("message", ["", "   \t\n"], ids=["empty", "whitespace"])
    async def test_send_message_refuses_blank_before_a_session(
        self, mock_context, message
    ):
        """A blank message is answered without acquiring a browser session.

        The extractor keeps the same guard, but it only runs once a session
        exists. Reaching it means `get_ready_extractor` has already had the
        chance to spend a login attempt and answer with an authentication
        error, which is not the refusal the caller can act on.
        """
        from linkedin_mcp_server.tools.messaging import register_messaging_tools

        mcp = FastMCP("test")
        register_messaging_tools(mcp)

        tool_fn = await get_tool_fn(mcp, "send_message")
        with patch(
            "linkedin_mcp_server.tools.messaging.get_ready_extractor",
            new_callable=AsyncMock,
        ) as ready:
            result = await tool_fn("testuser", message, True, mock_context)

        ready.assert_not_awaited()
        assert result["status"] == "invalid_message"
        assert result["sent"] is False
        # Nothing was submitted, so calling again cannot deliver twice.
        assert result["retry_safe"] is True
        assert result["url"] == "https://www.linkedin.com/in/testuser/"

    @pytest.mark.parametrize(
        "message",
        [f"First{chr(codepoint)}Second" for codepoint in (*range(32), 127)],
        ids=[f"U+{codepoint:04X}" for codepoint in (*range(32), 127)],
    )
    async def test_send_message_refuses_controls_before_a_session(
        self, mock_context, message
    ):
        from linkedin_mcp_server.tools.messaging import register_messaging_tools

        mcp = FastMCP("test")
        register_messaging_tools(mcp)

        tool_fn = await get_tool_fn(mcp, "send_message")
        with patch(
            "linkedin_mcp_server.tools.messaging.get_ready_extractor",
            new_callable=AsyncMock,
        ) as ready:
            result = await tool_fn("testuser", message, True, mock_context)

        ready.assert_not_awaited()
        assert result["status"] == "invalid_message"
        assert result["message"] == (
            "Message must not contain control characters or line breaks."
        )
        assert result["retry_safe"] is True

    @pytest.mark.parametrize(
        "username",
        ["", "me", "https://www.linkedin.com/company/microsoft/"],
        ids=["empty", "self-alias", "not-a-person"],
    )
    async def test_blank_message_with_an_unusable_recipient_is_mapped(
        self, mock_context, username
    ):
        """A recipient the refusal cannot name still reaches the error mapping.

        Building the refusal normalizes the recipient, so a username that
        cannot become a profile URL raises `InvalidReferenceError` from inside
        the guard. Raised past `raise_tool_error` it would arrive at the caller
        as a generic masked error (`mask_error_details=True` in `server.py`),
        which drops the one sentence that says what to correct.
        """
        from fastmcp.exceptions import ToolError

        from linkedin_mcp_server.core.exceptions import InvalidReferenceError
        from linkedin_mcp_server.tools.messaging import register_messaging_tools

        mcp = FastMCP("test")
        register_messaging_tools(mcp)

        tool_fn = await get_tool_fn(mcp, "send_message")
        with (
            patch(
                "linkedin_mcp_server.tools.messaging.get_ready_extractor",
                new_callable=AsyncMock,
            ) as ready,
            pytest.raises(ToolError) as excinfo,
        ):
            await tool_fn(username, "", True, mock_context)

        ready.assert_not_awaited()
        # A ToolError is what `mask_error_details` lets through, so the
        # correction the message names reaches the caller intact.
        cause = excinfo.value.__cause__
        assert isinstance(cause, InvalidReferenceError)
        assert str(excinfo.value) == str(cause) != ""

    @pytest.mark.parametrize(
        ("result", "warns"),
        [
            ({"status": "sent", "sent": True, "retry_safe": False}, True),
            ({"status": "send_unconfirmed", "sent": False, "retry_safe": False}, True),
            ({"status": "composer_occupied", "sent": False, "retry_safe": True}, False),
        ],
        ids=["sent", "unconfirmed", "refused"],
    )
    async def test_cancelled_completion_notification_warns(
        self, mock_context, caplog, result, warns
    ):
        """The last await can discard an answer that says a message went out.

        `ctx.report_progress` is the final await inside FastMCP's
        `anyio.fail_after()`, so a deadline landing there raises
        `CancelledError` past `except Exception` and throws away the result
        the send already produced. Nothing can hand it back afterwards, and
        the log line is then the only record.

        Silent where the result says a retry is safe: nothing was submitted,
        so there is no duplicate delivery to warn about. That is `retry_safe`
        and not the status, which is why a confirmed send is parametrized
        here alongside an unconfirmed one.
        """
        from linkedin_mcp_server.tools.messaging import register_messaging_tools

        mock_extractor = _make_mock_extractor({})
        # Only shapes the extractor can actually return. A confirmed send is
        # the one that most needs the warning and the one an implementation
        # keyed on `status == "send_unconfirmed"` would silently drop.
        mock_extractor.send_message = AsyncMock(
            return_value={
                "url": "https://www.linkedin.com/messaging/compose/",
                **result,
            }
        )
        # Only the completion notification is cancelled; the one before the
        # send has to pass or the send never runs.
        mock_context.report_progress = AsyncMock(
            side_effect=[None, asyncio.CancelledError()]
        )

        mcp = FastMCP("test")
        register_messaging_tools(mcp)
        tool_fn = await get_tool_fn(mcp, "send_message")

        with (
            patch(
                "linkedin_mcp_server.tools.messaging.get_ready_extractor",
                new_callable=AsyncMock,
                return_value=mock_extractor,
            ),
            caplog.at_level(
                logging.WARNING, logger="linkedin_mcp_server.tools.messaging"
            ),
            pytest.raises(asyncio.CancelledError),
        ):
            await tool_fn("testuser", "Hello!", True, mock_context)

        mock_extractor.send_message.assert_awaited_once()
        warnings = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
        assert (SEND_INTERRUPTED_WARNING in warnings) is warns, warnings

    async def test_send_message_with_profile_urn(self, mock_context, serve_extractor):
        expected = {
            "url": "https://www.linkedin.com/messaging/thread/abc123/",
            "status": "sent",
            "message": "Message sent.",
            "recipient_selected": True,
            "sent": True,
        }
        mock_extractor = _make_mock_extractor(expected)

        from linkedin_mcp_server.tools.messaging import register_messaging_tools

        mcp = FastMCP("test")
        register_messaging_tools(mcp)

        serve_extractor(mock_extractor)
        tool_fn = await get_tool_fn(mcp, "send_message")
        result = await tool_fn(
            "testuser",
            "Hello!",
            True,
            mock_context,
            profile_urn=" ACoAAB1IelEB ",
        )

        assert result["status"] == "sent"
        mock_extractor.send_message.assert_awaited_once_with(
            "testuser", "Hello!", confirm_send=True, profile_urn="ACoAAB1IelEB"
        )

        mock_extractor.send_message.reset_mock()
        await tool_fn(
            "testuser",
            "Hello!",
            True,
            mock_context,
            profile_urn="urn:li:fsd_profile:ACoAAB1IelEB",
        )
        mock_extractor.send_message.assert_awaited_once_with(
            "testuser",
            "Hello!",
            confirm_send=True,
            profile_urn="urn:li:fsd_profile:ACoAAB1IelEB",
        )

    async def test_send_message_error(self, mock_context, serve_extractor):
        from fastmcp.exceptions import ToolError

        from linkedin_mcp_server.exceptions import SessionExpiredError

        mock_extractor = MagicMock()
        mock_extractor.send_message = AsyncMock(side_effect=SessionExpiredError())

        from linkedin_mcp_server.tools.messaging import register_messaging_tools

        mcp = FastMCP("test")
        register_messaging_tools(mcp)

        serve_extractor(mock_extractor)
        tool_fn = await get_tool_fn(mcp, "send_message")
        with pytest.raises(ToolError, match="Session expired"):
            await tool_fn(
                "testuser",
                "Hello!",
                True,
                mock_context,
            )


class TestGetMyProfileTool:
    async def test_get_my_profile_success(self, mock_context, serve_extractor):
        expected = {
            "url": "https://www.linkedin.com/in/johndoe/",
            "sections": {"main_profile": "John Doe\nSoftware Engineer"},
        }
        mock_extractor = _make_mock_extractor(expected)

        from linkedin_mcp_server.tools.person import register_person_tools

        mcp = FastMCP("test")
        register_person_tools(mcp)

        serve_extractor(mock_extractor)
        tool_fn = await get_tool_fn(mcp, "get_my_profile")
        result = await tool_fn(mock_context)
        assert result["url"] == "https://www.linkedin.com/in/johndoe/"
        assert "main_profile" in result["sections"]
        mock_extractor.get_my_profile.assert_awaited_once()

    async def test_get_my_profile_with_sections(self, mock_context, serve_extractor):
        expected = {
            "url": "https://www.linkedin.com/in/johndoe/",
            "sections": {"main_profile": "John Doe", "experience": "Work history"},
        }
        mock_extractor = _make_mock_extractor(expected)

        from linkedin_mcp_server.tools.person import register_person_tools

        mcp = FastMCP("test")
        register_person_tools(mcp)

        serve_extractor(mock_extractor)
        tool_fn = await get_tool_fn(mcp, "get_my_profile")
        result = await tool_fn(mock_context, sections="experience")
        assert "main_profile" in result["sections"]
        assert "experience" in result["sections"]
        call_kwargs = mock_extractor.get_my_profile.call_args.kwargs
        assert "experience" in call_kwargs["sections"]

    async def test_get_my_profile_passes_callbacks(self, mock_context, serve_extractor):
        expected = {
            "url": "https://www.linkedin.com/in/johndoe/",
            "sections": {"main_profile": "John Doe"},
        }
        mock_extractor = _make_mock_extractor(expected)

        from linkedin_mcp_server.tools.person import register_person_tools

        mcp = FastMCP("test")
        register_person_tools(mcp)

        serve_extractor(mock_extractor)
        tool_fn = await get_tool_fn(mcp, "get_my_profile")
        await tool_fn(mock_context)

        call_kwargs = mock_extractor.get_my_profile.call_args.kwargs
        assert "callbacks" in call_kwargs
        assert isinstance(call_kwargs["callbacks"], MCPContextProgressCallback)

    async def test_get_my_profile_unknown_section(self, mock_context, serve_extractor):
        expected = {
            "url": "https://www.linkedin.com/in/johndoe/",
            "sections": {"main_profile": "John Doe"},
        }
        mock_extractor = _make_mock_extractor(expected)

        from linkedin_mcp_server.tools.person import register_person_tools

        mcp = FastMCP("test")
        register_person_tools(mcp)

        serve_extractor(mock_extractor)
        tool_fn = await get_tool_fn(mcp, "get_my_profile")
        result = await tool_fn(mock_context, sections="bogus_section")
        assert result["unknown_sections"] == ["bogus_section"]

    async def test_get_my_profile_error(self, mock_context, serve_extractor):
        from fastmcp.exceptions import ToolError

        from linkedin_mcp_server.exceptions import SessionExpiredError

        mock_extractor = MagicMock()
        mock_extractor.get_my_profile = AsyncMock(side_effect=SessionExpiredError())

        from linkedin_mcp_server.tools.person import register_person_tools

        mcp = FastMCP("test")
        register_person_tools(mcp)

        serve_extractor(mock_extractor)
        tool_fn = await get_tool_fn(mcp, "get_my_profile")
        with pytest.raises(ToolError, match="Session expired"):
            await tool_fn(mock_context)


class TestSearchCompaniesTool:
    async def test_search_companies_success(self, mock_context, serve_extractor):
        expected = {
            "url": "https://www.linkedin.com/search/results/companies/?keywords=fintech",
            "sections": {"search_results": "Stripe\nFintech company\nSan Francisco"},
        }
        mock_extractor = _make_mock_extractor(expected)

        from linkedin_mcp_server.tools.company import register_company_tools

        mcp = FastMCP("test")
        register_company_tools(mcp)

        serve_extractor(mock_extractor)
        tool_fn = await get_tool_fn(mcp, "search_companies")
        result = await tool_fn("fintech", mock_context)
        assert "search_results" in result["sections"]
        mock_extractor.search_companies.assert_awaited_once_with("fintech")

    async def test_search_companies_error(self, mock_context, serve_extractor):
        from fastmcp.exceptions import ToolError

        from linkedin_mcp_server.exceptions import SessionExpiredError

        mock_extractor = MagicMock()
        mock_extractor.search_companies = AsyncMock(side_effect=SessionExpiredError())

        from linkedin_mcp_server.tools.company import register_company_tools

        mcp = FastMCP("test")
        register_company_tools(mcp)

        serve_extractor(mock_extractor)
        tool_fn = await get_tool_fn(mcp, "search_companies")
        with pytest.raises(ToolError, match="Session expired"):
            await tool_fn("fintech", mock_context)


class TestGetCompanyEmployeesTool:
    async def test_get_company_employees_success(self, mock_context, serve_extractor):
        expected = {
            "url": "https://www.linkedin.com/company/anthropic/people/",
            "sections": {"employees": "Jane Doe\nResearch Engineer\nSan Francisco"},
        }
        mock_extractor = _make_mock_extractor(expected)

        from linkedin_mcp_server.tools.company import register_company_tools

        mcp = FastMCP("test")
        register_company_tools(mcp)

        serve_extractor(mock_extractor)
        tool_fn = await get_tool_fn(mcp, "get_company_employees")
        result = await tool_fn(
            "https://www.linkedin.com/company/anthropic/",
            mock_context,
        )
        assert "employees" in result["sections"]
        mock_extractor.get_company_employees.assert_awaited_once_with(
            "https://www.linkedin.com/company/anthropic/", keywords=None
        )

    async def test_get_company_employees_with_keywords(
        self, mock_context, serve_extractor
    ):
        expected = {
            "url": "https://www.linkedin.com/company/anthropic/people/?keywords=engineer",
            "sections": {"employees": "Jane Doe\nResearch Engineer"},
        }
        mock_extractor = _make_mock_extractor(expected)

        from linkedin_mcp_server.tools.company import register_company_tools

        mcp = FastMCP("test")
        register_company_tools(mcp)

        serve_extractor(mock_extractor)
        tool_fn = await get_tool_fn(mcp, "get_company_employees")
        result = await tool_fn("anthropic", mock_context, keywords="engineer")
        assert "employees" in result["sections"]
        mock_extractor.get_company_employees.assert_awaited_once_with(
            "anthropic", keywords="engineer"
        )

    async def test_get_company_employees_error(self, mock_context, serve_extractor):
        from fastmcp.exceptions import ToolError

        from linkedin_mcp_server.exceptions import SessionExpiredError

        mock_extractor = MagicMock()
        mock_extractor.get_company_employees = AsyncMock(
            side_effect=SessionExpiredError()
        )

        from linkedin_mcp_server.tools.company import register_company_tools

        mcp = FastMCP("test")
        register_company_tools(mcp)

        serve_extractor(mock_extractor)
        tool_fn = await get_tool_fn(mcp, "get_company_employees")
        with pytest.raises(ToolError, match="Session expired"):
            await tool_fn("anthropic", mock_context)


class TestFeedTools:
    async def test_get_feed_success(self, mock_context, serve_extractor):
        mock_extractor = MagicMock()
        mock_extractor.extract_feed = AsyncMock(
            return_value=ExtractedSection(text="Post 1\nPost 2", references=[])
        )

        from linkedin_mcp_server.tools.feed import register_feed_tools

        mcp = FastMCP("test")
        register_feed_tools(mcp)

        serve_extractor(mock_extractor)
        tool_fn = await get_tool_fn(mcp, "get_feed")
        result = await tool_fn(mock_context)
        assert result["url"] == "https://www.linkedin.com/feed/"
        assert "feed" in result["sections"]
        assert result["sections"]["feed"] == "Post 1\nPost 2"
        assert "posts" not in result

    async def test_get_feed_surfaces_references(self, mock_context, serve_extractor):
        """References from the extractor flow through to the tool result."""
        mock_extractor = MagicMock()
        mock_extractor.extract_feed = AsyncMock(
            return_value=ExtractedSection(
                text="Some feed text",
                references=[
                    {
                        "kind": "feed_post",
                        "url": "/posts/alice_hello-ugcPost-1-xx",
                        "context": "feed",
                    },
                    {
                        "kind": "feed_post",
                        "url": "/feed/update/urn:li:activity:1234567890/",
                    },
                ],
            )
        )

        from linkedin_mcp_server.tools.feed import register_feed_tools

        mcp = FastMCP("test")
        register_feed_tools(mcp)

        serve_extractor(mock_extractor)
        tool_fn = await get_tool_fn(mcp, "get_feed")
        result = await tool_fn(mock_context)
        assert "posts" not in result
        assert "feed" in result["references"]
        urls = [r["url"] for r in result["references"]["feed"]]
        assert "/posts/alice_hello-ugcPost-1-xx" in urls
        assert "/feed/update/urn:li:activity:1234567890/" in urls

    async def test_get_feed_suppresses_references_without_readable_text(
        self, mock_context, serve_extractor
    ):
        mock_extractor = MagicMock()
        mock_extractor.extract_feed = AsyncMock(
            return_value=ExtractedSection(
                text="",
                references=[
                    {
                        "kind": "feed_post",
                        "url": "/posts/orphan-ugcPost-1-xx",
                        "context": "feed",
                    }
                ],
            )
        )

        from linkedin_mcp_server.tools.feed import register_feed_tools

        mcp = FastMCP("test")
        register_feed_tools(mcp)

        serve_extractor(mock_extractor)
        tool_fn = await get_tool_fn(mcp, "get_feed")
        result = await tool_fn(mock_context)
        assert result == {"url": "https://www.linkedin.com/feed/", "sections": {}}

    async def test_get_feed_rate_limited_surfaces_section_error(
        self, mock_context, serve_extractor
    ):
        """Rate-limit sentinel becomes a typed section_errors entry."""
        mock_extractor = MagicMock()
        mock_extractor.extract_feed = AsyncMock(
            return_value=ExtractedSection(text=RATE_LIMITED_SECTION_TEXT, references=[])
        )

        from linkedin_mcp_server.tools.feed import register_feed_tools

        mcp = FastMCP("test")
        register_feed_tools(mcp)

        serve_extractor(mock_extractor)
        tool_fn = await get_tool_fn(mcp, "get_feed")
        result = await tool_fn(mock_context)
        assert "feed" not in result["sections"]
        assert result["section_errors"]["feed"]["error_type"] == "rate_limit"
        assert (
            result["section_errors"]["feed"]["error_message"]
            == RATE_LIMITED_SECTION_TEXT
        )

    async def test_get_feed_returns_section_errors(self, mock_context, serve_extractor):
        mock_extractor = MagicMock()
        mock_extractor.extract_feed = AsyncMock(
            return_value=ExtractedSection(
                text="",
                references=[],
                error={"issue_template_path": "/tmp/feed-issue.md"},
            )
        )

        from linkedin_mcp_server.tools.feed import register_feed_tools

        mcp = FastMCP("test")
        register_feed_tools(mcp)

        serve_extractor(mock_extractor)
        tool_fn = await get_tool_fn(mcp, "get_feed")
        result = await tool_fn(mock_context)
        assert result["sections"] == {}
        assert "feed" in result["section_errors"]

    async def test_get_feed_rejects_zero_num_posts(self, mock_context):
        """Verify num_posts=0 is rejected by Field(ge=1) validation."""
        # FastMCP wraps the pydantic error raised by Field() constraints in
        # its own ValidationError, which does not subclass pydantic's.
        from fastmcp.exceptions import ValidationError

        from linkedin_mcp_server.tools.feed import register_feed_tools

        mcp = FastMCP("test")
        register_feed_tools(mcp)

        with pytest.raises(ValidationError, match="num_posts"):
            await mcp.call_tool("get_feed", {"num_posts": 0})

    async def test_get_feed_rejects_excessive_num_posts(self, mock_context):
        """Verify num_posts=51 is rejected by Field(le=50) validation."""
        # FastMCP wraps the pydantic error raised by Field() constraints in
        # its own ValidationError, which does not subclass pydantic's.
        from fastmcp.exceptions import ValidationError

        from linkedin_mcp_server.tools.feed import register_feed_tools

        mcp = FastMCP("test")
        register_feed_tools(mcp)

        with pytest.raises(ValidationError, match="num_posts"):
            await mcp.call_tool("get_feed", {"num_posts": 51})


class TestPostTools:
    async def test_search_posts_success(self, mock_context, serve_extractor):
        expected = {
            "url": (
                "https://www.linkedin.com/search/results/content/"
                "?keywords=Buscamos+Unity&origin=FACETED_SEARCH"
            ),
            "sections": {"search_results": "Acme is hiring a Unity dev!"},
        }
        mock_extractor = _make_mock_extractor(expected)

        from linkedin_mcp_server.tools.post import register_post_tools

        mcp = FastMCP("test")
        register_post_tools(mcp)

        serve_extractor(mock_extractor)
        tool_fn = await get_tool_fn(mcp, "search_posts")
        result = await tool_fn(
            "Buscamos Unity",
            mock_context,
            date_posted="past-week",
        )
        assert "search_results" in result["sections"]
        mock_extractor.search_posts.assert_awaited_once_with(
            "Buscamos Unity",
            date_posted="past-week",
            max_pages=3,
        )

    async def test_search_posts_validation_error_surfaced_as_tool_error(
        self, mock_context, serve_extractor
    ):
        """A FilterValidationError from the extractor surfaces to the client as
        a ToolError carrying the same message, not the generic mask."""
        from fastmcp.exceptions import ToolError

        from linkedin_mcp_server.scraping.contracts import FilterValidationError
        from linkedin_mcp_server.tools.post import register_post_tools

        mock_extractor = MagicMock()
        mock_extractor.search_posts = AsyncMock(
            side_effect=FilterValidationError("Invalid date_posted 'last-year'")
        )

        mcp = FastMCP("test")
        register_post_tools(mcp)
        serve_extractor(mock_extractor)
        tool_fn = await get_tool_fn(mcp, "search_posts")

        with pytest.raises(ToolError, match="Invalid date_posted"):
            await tool_fn(
                "python",
                mock_context,
                date_posted="last-year",
            )

    async def test_search_posts_rejects_zero_max_pages(self, mock_context):
        """Verify max_pages=0 is rejected by Field(ge=1) validation."""
        # FastMCP wraps the pydantic error raised by Field() constraints in
        # its own ValidationError, which does not subclass pydantic's.
        from fastmcp.exceptions import ValidationError

        from linkedin_mcp_server.tools.post import register_post_tools

        mcp = FastMCP("test")
        register_post_tools(mcp)

        with pytest.raises(ValidationError, match="max_pages"):
            await mcp.call_tool("search_posts", {"keywords": "python", "max_pages": 0})


class TestToolTimeouts:
    async def test_all_tools_have_global_timeout(self):
        from linkedin_mcp_server.server import create_mcp_server

        custom_timeout = 7.5
        mcp = create_mcp_server(tool_timeout=custom_timeout)

        tool_names = (
            "get_person_profile",
            "connect_with_person",
            "get_sidebar_profiles",
            "search_people",
            "get_company_profile",
            "get_company_posts",
            "get_job_details",
            "search_jobs",
            "get_saved_jobs",
            "get_inbox",
            "get_conversation",
            "search_conversations",
            "send_message",
            "get_feed",
            "search_posts",
            "close_session",
        )

        for name in tool_names:
            tool = await mcp.get_tool(name)
            assert tool is not None
            assert tool.timeout == custom_timeout

    async def test_all_tools_have_default_timeout(self):
        from linkedin_mcp_server.config.schema import DEFAULT_TOOL_TIMEOUT_SECONDS
        from linkedin_mcp_server.server import create_mcp_server

        mcp = create_mcp_server()

        tool_names = (
            "get_person_profile",
            "get_my_profile",
            "connect_with_person",
            "get_sidebar_profiles",
            "search_people",
            "get_company_profile",
            "get_company_posts",
            "search_companies",
            "get_company_employees",
            "get_job_details",
            "search_jobs",
            "get_saved_jobs",
            "get_inbox",
            "get_conversation",
            "search_conversations",
            "send_message",
            "get_feed",
            "search_posts",
            "close_session",
        )

        for name in tool_names:
            tool = await mcp.get_tool(name)
            assert tool is not None
            assert tool.timeout == DEFAULT_TOOL_TIMEOUT_SECONDS
