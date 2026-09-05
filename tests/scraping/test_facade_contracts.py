"""Public extractor facade contracts frozen before decomposition."""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import inspect

from fastmcp.tools import FunctionTool
from patchright.async_api import Page

from linkedin_mcp_server import dependencies
from linkedin_mcp_server.scraping import LinkedInExtractor as PackageExtractor
from linkedin_mcp_server.scraping import contracts, text
from linkedin_mcp_server.scraping.extractor import (
    ExtractedSection,
    FilterValidationError,
    LinkedInExtractor,
    rate_limited_section_error,
    strip_conversation_chrome,
    strip_linkedin_noise,
)
from linkedin_mcp_server.server import create_mcp_server

from .policy_scenarios import COMPATIBILITY_METHODS, TOOL_FACADE_METHODS
from .support.policy_trace import ScriptedPage, TraceRecorder


TOOL_DELEGATES = {
    "connect_with_person": "connect_with_person",
    "get_company_employees": "get_company_employees",
    "get_company_posts": "extract_page",
    "get_company_profile": "scrape_company",
    "get_conversation": "get_conversation",
    "get_feed": "extract_feed",
    "get_inbox": "get_inbox",
    "get_job_details": "scrape_job",
    "get_my_profile": "get_my_profile",
    "get_person_profile": "scrape_person",
    "get_saved_jobs": "get_saved_jobs",
    "get_sidebar_profiles": "get_sidebar_profiles",
    "search_companies": "search_companies",
    "search_conversations": "search_conversations",
    "search_jobs": "search_jobs",
    "search_people": "search_people",
    "search_posts": "search_posts",
    "send_message": "send_message",
}


async def test_constructor_export_and_dependency_use_the_same_facade(monkeypatch):
    recorder = TraceRecorder("facade-construction", set())
    page = ScriptedPage(recorder)
    extractor = LinkedInExtractor(cast(Page, page))
    calls: list[str] = []

    async def ready(tool_name: str, ctx: object) -> None:
        assert tool_name == "policy-test"
        assert ctx is None
        calls.append("ready")

    async def browser() -> SimpleNamespace:
        calls.append("browser")
        return SimpleNamespace(page=page)

    async def authenticated() -> None:
        calls.append("authenticated")

    monkeypatch.setattr(dependencies, "ensure_tool_ready_or_raise", ready)
    monkeypatch.setattr(dependencies, "get_or_create_browser", browser)
    monkeypatch.setattr(dependencies, "ensure_authenticated", authenticated)

    constructed = await dependencies.get_ready_extractor(None, tool_name="policy-test")

    assert PackageExtractor is LinkedInExtractor
    assert extractor._page is page
    assert extractor._session.page is page
    assert extractor._navigator._session is extractor._session
    assert type(constructed) is LinkedInExtractor
    assert constructed._page is page
    assert constructed._session.page is page
    assert constructed._navigator._session is constructed._session
    assert calls == ["ready", "browser", "authenticated"]


def test_permanent_facade_aliases_are_the_canonical_objects():
    # Identity, not equality: an alias that merely compares equal would let a
    # second copy of a contract live in the facade, and `except
    # FilterValidationError` imported from one copy would not catch the other.
    assert ExtractedSection is contracts.ExtractedSection
    assert FilterValidationError is contracts.FilterValidationError
    assert rate_limited_section_error is contracts.rate_limited_section_error
    assert strip_linkedin_noise is text.strip_linkedin_noise
    assert strip_conversation_chrome is text.strip_conversation_chrome


async def test_registered_tools_and_extractor_delegates_are_counted_separately():
    tools = await create_mcp_server().list_tools()
    tool_names = {tool.name for tool in tools}

    assert len(tool_names) == 19
    assert tool_names == {*TOOL_DELEGATES, "close_session"}
    assert len(TOOL_DELEGATES) == 18
    assert set(TOOL_DELEGATES.values()) == TOOL_FACADE_METHODS
    assert "close_session" not in TOOL_DELEGATES


async def test_company_posts_delegate_matches_registered_tool_consumer():
    tool = await create_mcp_server().get_tool("get_company_posts")
    assert isinstance(tool, FunctionTool)
    extractor = SimpleNamespace(
        extract_page=AsyncMock(
            return_value=SimpleNamespace(text="posts", references=[], error=None)
        ),
        scrape_company=AsyncMock(),
    )
    context = SimpleNamespace(report_progress=AsyncMock())

    await tool.fn("example", context, extractor=extractor)

    delegate = getattr(extractor, TOOL_DELEGATES["get_company_posts"])
    delegate.assert_awaited_once()
    extractor.extract_page.assert_awaited_once_with(
        "https://www.linkedin.com/company/example/posts/", section_name="posts"
    )
    extractor.scrape_company.assert_not_awaited()


def test_facade_methods_are_exactly_the_frozen_coroutine_surface():
    expected = TOOL_FACADE_METHODS | COMPATIBILITY_METHODS
    # Enumerate the class, not the expectation: iterating over `expected` would
    # let a newly added public coroutine pass unnoticed.
    actual = {
        name
        for name in dir(LinkedInExtractor)
        if not name.startswith("_")
        and inspect.iscoroutinefunction(getattr(LinkedInExtractor, name))
    }

    assert actual == expected
    assert len(TOOL_FACADE_METHODS) == 18
    assert len(COMPATIBILITY_METHODS) == 2


async def test_compatibility_helpers_keep_their_browser_behavior():
    allowed = {
        "evaluate",
        "locator.click",
        "locator.count",
        "locator.create",
        "locator.derive",
        "locator.scroll_into_view",
    }
    recorder = TraceRecorder("compatibility-helpers", allowed)
    page = ScriptedPage(recorder).script("evaluate:page_text", "Policy text")
    page.declare_locator("main", "main-scope")
    page.declare_locator(
        "button, a, [role='button']", "click-candidates", parent="main-scope"
    )
    page.declare_derived(
        "click-candidates", "filter:^Connect$/re.UNICODE", "exact-connect"
    )
    page.declare_derived("exact-connect", "first", "connect-target")
    page.script("exact-connect.count", 1)
    page.script("connect-target.scroll_into_view", None)
    page.script("connect-target.click", None)
    extractor = LinkedInExtractor(cast(Page, page))

    assert await extractor.get_page_text() == "Policy text"
    assert await extractor.click_button_by_text("Connect") is True
    assert [event["kind"] for event in recorder.events] == [
        "evaluate",
        "locator.create",
        "locator.create",
        "locator.derive",
        "locator.count",
        "locator.derive",
        "locator.scroll_into_view",
        "locator.click",
    ]
