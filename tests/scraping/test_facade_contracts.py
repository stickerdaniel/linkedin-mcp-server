"""Public extractor facade contracts frozen before decomposition."""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, patch

import inspect

import pytest

from fastmcp.tools import FunctionTool
from patchright.async_api import Page

from linkedin_mcp_server import dependencies
from linkedin_mcp_server.core.exceptions import InvalidReferenceError
from linkedin_mcp_server.scraping import LinkedInExtractor as PackageExtractor
from linkedin_mcp_server.scraping import connection, contracts, text
from linkedin_mcp_server.scraping.capture import SectionCapture
from linkedin_mcp_server.scraping.connection import ActionSignals
from linkedin_mcp_server.scraping.connection_actions import ConnectionActions
from linkedin_mcp_server.scraping.content import PageContentReader
from linkedin_mcp_server.scraping.conversations import ConversationReader
from linkedin_mcp_server.scraping.extractor import (
    ExtractedSection,
    FilterValidationError,
    LinkedInExtractor,
    rate_limited_section_error,
    strip_conversation_chrome,
    strip_linkedin_noise,
)
from linkedin_mcp_server.scraping.jobs import JobScraper
from linkedin_mcp_server.scraping.message_sender import MessageSender
from linkedin_mcp_server.scraping.navigation import PageNavigator
from linkedin_mcp_server.scraping.profile_page import ProfilePageReader
from linkedin_mcp_server.scraping.session import ScrapingSession
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
    assert type(constructed) is LinkedInExtractor
    assert constructed._page is page
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


async def test_facade_scrape_person_forwards_its_keyword_only_arguments(mock_page):
    # No production caller passes either one to the facade any more: the
    # redirect path that drove `allow_self_alias` through it moved to the
    # person owner, which calls its own `scrape_person`. Replacing both
    # forwards with `False` survived the whole suite, so the delegate is
    # pinned here, against the real owner rather than a mock of it.
    mock_page.url = "https://www.linkedin.com/in/me/"
    extractor = LinkedInExtractor(cast(Page, mock_page))
    section = ExtractedSection(text="reused", references=[], error=None)

    with (
        patch.object(
            SectionCapture,
            "_extract_loaded_section",
            new_callable=AsyncMock,
            return_value=section,
        ) as loaded,
        patch.object(
            SectionCapture,
            "extract_page",
            new_callable=AsyncMock,
            return_value=section,
        ) as extract_page,
    ):
        result = await extractor.scrape_person(
            "me",
            {"main_profile"},
            main_profile_already_loaded=True,
            allow_self_alias=True,
        )

    # Reaching this line at all is what `allow_self_alias` buys; reusing the
    # loaded page instead of navigating is what the other one buys.
    assert result["url"] == "https://www.linkedin.com/in/me/"
    loaded.assert_awaited_once()
    extract_page.assert_not_awaited()


async def test_facade_connect_forwards_the_optional_note(mock_page):
    # The owner tests call ConnectionActions directly. This pins the facade
    # boundary, where dropping `note=note` silently turns a personalized
    # request into an invitation without its requested note.
    extractor = LinkedInExtractor(cast(Page, mock_page))
    expected = {"url": "https://www.linkedin.com/in/target/", "status": "pending"}

    with patch.object(
        ConnectionActions,
        "connect_with_person",
        new_callable=AsyncMock,
        return_value=expected,
    ) as connect:
        result = await extractor.connect_with_person("target", note="context")

    assert result is expected
    connect.assert_awaited_once_with("target", note="context")


async def test_connection_profile_read_resolves_the_facade_delegate_late(mock_page):
    # Composition deliberately uses a lambda rather than a captured bound
    # method. A replacement installed after construction must still receive
    # the main-profile read that starts the connection workflow.
    extractor = LinkedInExtractor(cast(Page, mock_page))
    replacement = AsyncMock(
        return_value={
            "url": "https://www.linkedin.com/in/target/",
            "sections": {"main_profile": "Target profile"},
        }
    )
    extractor.scrape_person = replacement  # ty: ignore[invalid-assignment]
    self_profile = ActionSignals(False, False, True, False, False, False)

    with patch.object(
        ConnectionActions,
        "_read_action_signals",
        new_callable=AsyncMock,
        return_value=self_profile,
    ):
        result = await extractor.connect_with_person("target")

    assert result["status"] == "connect_unavailable"
    replacement.assert_awaited_once_with("target", {"main_profile"})


async def test_facade_search_posts_forwards_its_recency_filter(mock_page):
    # The scroll depth is held by the `search-posts` trace, which runs the
    # facade with `max_pages=2` and records the scrolls it buys. The recency
    # filter is not: no scenario passes one, and replacing the forward with
    # `None` survived the whole suite. Pinned here against the real owner,
    # because dropping it answers a filtered request with unfiltered results
    # under a URL that says otherwise.
    extractor = LinkedInExtractor(cast(Page, mock_page))

    with patch.object(
        SectionCapture,
        "extract_page",
        new_callable=AsyncMock,
        return_value=ExtractedSection(text="post", references=[], error=None),
    ):
        result = await extractor.search_posts("unity", date_posted="past-week")

    assert "datePosted=%5B%22past-week%22%5D" in result["url"]
    with pytest.raises(FilterValidationError):
        await extractor.search_posts("unity", date_posted="last-year")


async def test_facade_search_jobs_forwards_every_filter_in_order(mock_page):
    # Several filters have the same type, so a positional swap is valid Python
    # and changes the query silently. That exact mutation survived the full
    # suite before this boundary assertion was added.
    extractor = LinkedInExtractor(cast(Page, mock_page))
    expected = {"url": "https://www.linkedin.com/jobs/search/", "sections": {}}

    with patch.object(
        JobScraper,
        "search_jobs",
        new_callable=AsyncMock,
        return_value=expected,
    ) as search_jobs:
        result = await extractor.search_jobs(
            "python",
            "Berlin",
            2,
            "past-week",
            "full-time",
            "mid-senior",
            "remote",
            True,
            "recent",
            17.5,
        )

    assert result is expected
    search_jobs.assert_awaited_once_with(
        "python",
        "Berlin",
        2,
        "past-week",
        "full-time",
        "mid-senior",
        "remote",
        True,
        "recent",
        17.5,
    )


async def test_facade_get_conversation_forwards_its_username_and_index(mock_page):
    # The `conversation` trace drives this delegate by `thread_id` alone, so
    # neither of the other two arguments is pinned there: replacing both
    # forwards with their defaults survives every trace. Pinned here against
    # the real owner, because dropping the username answers a by-participant
    # request with "Provide at least one of ...", and dropping the index
    # answers it with somebody's most recent thread instead of the one asked
    # for.
    extractor = LinkedInExtractor(cast(Page, mock_page))
    threads = [
        "https://www.linkedin.com/messaging/thread/2-newer/",
        "https://www.linkedin.com/messaging/thread/2-older/",
    ]

    with (
        patch.object(ScrapingSession, "check_rate_limit", new_callable=AsyncMock),
        patch.object(ScrapingSession, "dismiss_modal", new_callable=AsyncMock),
        patch.object(ScrapingSession, "delay", new_callable=AsyncMock),
        patch.object(
            PageNavigator, "_navigate_to_page", new_callable=AsyncMock
        ) as navigate,
        patch.object(
            ProfilePageReader,
            "_read_profile_display_name",
            new_callable=AsyncMock,
            return_value="Jacki McMahan",
        ),
        patch.object(
            ConversationReader,
            "_resolve_conversation_thread_urls",
            new_callable=AsyncMock,
            return_value=threads,
        ),
        patch.object(
            PageContentReader,
            "_extract_root_content",
            new_callable=AsyncMock,
            return_value={"source": "root", "text": "msg", "references": []},
        ),
    ):
        await extractor.get_conversation(linkedin_username="jacki-old", index=1)

    expected_navigations = [
        "https://www.linkedin.com/in/jacki-old/",
        threads[1],
    ]
    assert [call.args[0] for call in navigate.await_args_list] == expected_navigations


async def test_facade_search_conversations_forwards_its_row_cap(mock_page):
    # `limit` reaches nothing the `search-conversations` trace records: the
    # scripted row wait times out, so the cap never gets as far as the click
    # loop it bounds. Replacing the forward with the default survives the
    # traces, and every row the loop visits may be marked read, which is the
    # side effect the cap exists to bound.
    extractor = LinkedInExtractor(cast(Page, mock_page))

    with (
        patch.object(ScrapingSession, "check_rate_limit", new_callable=AsyncMock),
        patch.object(ScrapingSession, "dismiss_modal", new_callable=AsyncMock),
        patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
        patch.object(ConversationReader, "_wait_for_main_text", new_callable=AsyncMock),
        patch.object(
            PageContentReader,
            "_extract_root_content",
            new_callable=AsyncMock,
            return_value={"source": "root", "text": "hit", "references": []},
        ),
        patch.object(
            ConversationReader,
            "_extract_conversation_thread_refs",
            new_callable=AsyncMock,
            return_value=[],
        ) as refs,
    ):
        await extractor.search_conversations("engine", limit=7)

    refs.assert_awaited_once_with(limit=7, context="search_results")


async def test_facade_scrape_person_keeps_refusing_the_self_alias_by_default(mock_page):
    # The other half of the forward above: without the argument `me` is a
    # reserved name, so the assertion that it scraped says something.
    extractor = LinkedInExtractor(cast(Page, mock_page))

    with pytest.raises(InvalidReferenceError):
        await extractor.scrape_person("me", {"main_profile"})


async def test_facade_send_message_forwards_every_argument(mock_page):
    extractor = LinkedInExtractor(cast(Page, mock_page))
    expected = {
        "url": "https://www.linkedin.com/in/target/",
        "status": "confirmation_required",
    }

    with patch.object(
        MessageSender,
        "send_message",
        new_callable=AsyncMock,
        return_value=expected,
    ) as send_message:
        result = await extractor.send_message(
            "target",
            "Message text",
            confirm_send=False,
            profile_urn="ACoAAB",
        )

    assert result is expected
    send_message.assert_awaited_once_with(
        "target",
        "Message text",
        confirm_send=False,
        profile_urn="ACoAAB",
    )


async def test_facade_direct_thread_ignores_username_and_index_validation(mock_page):
    # The direct route exists to bypass participant resolution. A malformed
    # username and negative index are both invalid on that other branch, so
    # reaching the named thread proves neither ignored value is checked eagerly.
    extractor = LinkedInExtractor(cast(Page, mock_page))

    with (
        patch.object(ScrapingSession, "check_rate_limit", new_callable=AsyncMock),
        patch.object(ScrapingSession, "dismiss_modal", new_callable=AsyncMock),
        patch.object(ScrapingSession, "delay", new_callable=AsyncMock),
        patch.object(
            PageNavigator, "_navigate_to_page", new_callable=AsyncMock
        ) as navigate,
        patch.object(ConversationReader, "_wait_for_main_text", new_callable=AsyncMock),
        patch.object(
            ConversationReader,
            "_scroll_main_scrollable_region",
            new_callable=AsyncMock,
        ),
        patch.object(
            PageContentReader,
            "_extract_root_content",
            new_callable=AsyncMock,
            return_value={"source": "root", "text": "msg", "references": []},
        ),
    ):
        result = await extractor.get_conversation(
            linkedin_username="../../feed", thread_id="2-direct", index=-1
        )

    assert result["sections"]["conversation"] == "msg"
    navigate.assert_awaited_once_with(
        "https://www.linkedin.com/messaging/thread/2-direct/"
    )


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


async def test_incoming_verification_resolves_classifier_at_call_time(
    mock_page, monkeypatch
):
    # Rebind after facade/action construction. Both the initial decision and the
    # post-accept verification must resolve the canonical owner dynamically.
    extractor = LinkedInExtractor(cast(Page, mock_page))
    extractor.scrape_person = AsyncMock(  # ty: ignore[invalid-assignment]
        return_value={
            "url": "https://www.linkedin.com/in/target/",
            "sections": {"main_profile": "Target profile"},
        }
    )
    incoming = ActionSignals(False, False, False, False, False, True)
    connected = ActionSignals(False, True, False, False, False, False)
    calls: list[ActionSignals] = []

    def classify(value: ActionSignals) -> connection.ConnectionState:
        calls.append(value)
        return "incoming_request" if value is incoming else "already_connected"

    monkeypatch.setattr(connection, "detect_connection_state", classify)
    with (
        patch.object(
            ConnectionActions,
            "_read_action_signals",
            new_callable=AsyncMock,
            side_effect=[incoming, connected],
        ),
        patch.object(
            ConnectionActions,
            "_click_incoming_accept",
            new_callable=AsyncMock,
            return_value=True,
        ),
    ):
        result = await extractor.connect_with_person("target")

    assert result["status"] == "accepted"
    assert calls == [incoming, connected]


async def test_submitted_invite_verification_resolves_classifier_at_call_time(
    mock_page, monkeypatch
):
    # The fake navigator and submitter keep this entirely off LinkedIn while the
    # verification branch still performs both classifier calls.
    extractor = LinkedInExtractor(cast(Page, mock_page))
    extractor.scrape_person = AsyncMock(  # ty: ignore[invalid-assignment]
        return_value={
            "url": "https://www.linkedin.com/in/target/",
            "sections": {"main_profile": "Target profile"},
        }
    )
    connectable = ActionSignals(True, False, False, False, False, False)
    pending = ActionSignals(False, True, False, False, True, False)
    calls: list[ActionSignals] = []

    def classify(value: ActionSignals) -> connection.ConnectionState:
        calls.append(value)
        return "connectable" if value is connectable else "pending"

    monkeypatch.setattr(connection, "detect_connection_state", classify)
    with (
        patch.object(
            ConnectionActions,
            "_read_action_signals",
            new_callable=AsyncMock,
            side_effect=[connectable, pending],
        ),
        patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
        patch.object(
            ConnectionActions,
            "_submit_invite_dialog",
            new_callable=AsyncMock,
            return_value=(True, False, None),
        ),
    ):
        result = await extractor.connect_with_person("target")

    assert result["status"] == "connected"
    assert calls == [connectable, pending]
