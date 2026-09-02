"""Canonical semantic scraping-policy scenarios."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import inspect
import json

from patchright.async_api import Page
from patchright.async_api import TimeoutError as PlaywrightTimeoutError

from linkedin_mcp_server.callbacks import ProgressCallback
from linkedin_mcp_server.scraping import extractor as extractor_module
from linkedin_mcp_server.scraping.extractor import LinkedInExtractor
from linkedin_mcp_server.scraping.fields import COMPANY_SECTIONS, PERSON_SECTIONS

from .support.policy_trace import (
    FakeClock,
    ScriptedPage,
    ScriptedResponse,
    TraceRecorder,
    bind_effective,
)


TRACE_ROOT = Path(__file__).parents[1] / "fixtures" / "scraping-policy" / "v1"

_COMMON_ALLOWED = {
    "boundary.auth_quick",
    "boundary.drain",
    "boundary.modal",
    "boundary.rate_limit",
    "boundary.scroll_body",
    "boundary.scroll_sidebar",
    "boundary.trace",
    "callback.complete",
    "callback.progress",
    "callback.start",
    "evaluate",
    "keyboard.press",
    "keyboard.type",
    "listener.add",
    "listener.emit",
    "listener.remove",
    "locator.count",
    "locator.create",
    "locator.derive",
    "locator.is_visible",
    "locator.wait_for",
    "mouse.move",
    "mouse.wheel",
    "navigate",
    "sleep",
    "wait_for_function",
    "wait_for_load_state",
    "wait_for_selector",
}


class TraceCallbacks(ProgressCallback):
    """Record progress callbacks without a mock object."""

    def __init__(self, recorder: TraceRecorder):
        self.recorder = recorder

    async def on_start(self, scraper_type: str, url: str) -> None:
        self.recorder.record("callback.start", operation=scraper_type, url=url)

    async def on_progress(self, message: str, percent: int) -> None:
        self.recorder.record("callback.progress", message=message, percent=percent)

    async def on_complete(self, scraper_type: str, result: Any) -> None:
        self.recorder.record(
            "callback.complete", operation=scraper_type, result_url=result["url"]
        )


@asynccontextmanager
async def boundaries(recorder: TraceRecorder, clock: FakeClock) -> AsyncIterator[None]:
    real_scroll_body = extractor_module.scroll_to_bottom
    real_scroll_sidebar = extractor_module.scroll_job_sidebar
    real_drain = extractor_module._drain_listener_tasks

    async def trace(_page: Any, label: str, *, extra: Any = None) -> None:
        recorder.record("boundary.trace", label=label, extra=extra)

    async def auth_quick(_page: Any) -> None:
        recorder.record("boundary.auth_quick", result=None)
        return None

    async def auth(_page: Any) -> None:
        return None

    async def remember(_page: Any) -> bool:
        return False

    async def stabilize(_description: str, _logger: Any) -> None:
        return None

    async def rate_limit(_page: Any) -> None:
        recorder.record("boundary.rate_limit")

    async def modal(_page: Any) -> bool:
        recorder.record("boundary.modal", dismissed=False)
        return False

    async def scroll_body(*args: Any, **kwargs: Any) -> None:
        values = bind_effective(real_scroll_body, *args, **kwargs)
        values.pop("page")
        recorder.record("boundary.scroll_body", **values, actual_scrolls=2)
        clock.advance(values["pause_time"] * 2)

    async def scroll_sidebar(*args: Any, **kwargs: Any) -> bool:
        values = bind_effective(real_scroll_sidebar, *args, **kwargs)
        values.pop("page")
        recorder.record(
            "boundary.scroll_sidebar", **values, actual_scrolls=2, moved=False
        )
        clock.advance(0.4)
        return False

    async def drain(tasks: list[Any]) -> None:
        # The real drain is what runs; the event only marks where it happens,
        # so swapping it with listener removal shows up as a reordered trace.
        recorder.record("boundary.drain", pending=len(tasks))
        await real_drain(tasks)

    def diagnostics(error: Exception, **values: Any) -> dict[str, Any]:
        return {
            "error_type": type(error).__name__,
            "error_message": str(error),
            "context": values.get("context"),
        }

    with (
        patch.object(extractor_module, "record_page_trace", trace),
        patch.object(extractor_module, "detect_auth_barrier_quick", auth_quick),
        patch.object(extractor_module, "detect_auth_barrier", auth),
        patch.object(extractor_module, "resolve_remember_me_prompt", remember),
        patch.object(extractor_module, "stabilize_navigation", stabilize),
        patch.object(extractor_module, "detect_rate_limit", rate_limit),
        patch.object(extractor_module, "handle_modal_close", modal),
        patch.object(extractor_module, "scroll_to_bottom", scroll_body),
        patch.object(extractor_module, "scroll_job_sidebar", scroll_sidebar),
        patch.object(extractor_module, "build_issue_diagnostics", diagnostics),
        patch.object(extractor_module, "_drain_listener_tasks", drain),
        patch.object(extractor_module.asyncio, "sleep", clock.sleep),
        patch.object(extractor_module.time, "monotonic", clock.monotonic),
    ):
        yield


def _page(recorder: TraceRecorder, *, url: str = "about:blank") -> ScriptedPage:
    return ScriptedPage(recorder, url=url).script("evaluate:root_content")


def _extractor(page: ScriptedPage) -> LinkedInExtractor:
    return LinkedInExtractor(cast(Page, page))


def _root(text: str, references: list[dict[str, str]] | None = None) -> dict[str, Any]:
    return {
        "source": "root",
        "text": text,
        "references": references or [],
    }


async def _generic_capture_scenario(
    name: str, url: str, *, max_scrolls: int | None = None
) -> dict[str, Any]:
    recorder = TraceRecorder(name, _COMMON_ALLOWED)
    clock = FakeClock(recorder)
    page = _page(recorder).script("evaluate:root_content", _root("Policy content"))
    extractor = _extractor(page)
    async with boundaries(recorder, clock):
        with recorder.context("extract_page", "section"):
            result = await extractor.extract_page(
                url, "section", max_scrolls=max_scrolls
            )
    page.assert_clean()
    return recorder.trace(
        {
            "method": "extract_page",
            "arguments": {
                "url": url,
                "section_name": "section",
                "max_scrolls": max_scrolls,
            },
        },
        {"text": result.text, "references": result.references},
    )


async def _person_sections_scenario() -> dict[str, Any]:
    name = "scrape_person__all_sections"
    recorder = TraceRecorder(name, _COMMON_ALLOWED)
    clock = FakeClock(recorder)
    page = _page(recorder)
    # 13 valid company anchors against the documented cap of 12 for a section,
    # so dropping the cap shows up as a fourteenth reference in the trace.
    overflowing = [
        {
            "href": f"https://www.linkedin.com/company/policy-employer-{index}/",
            "text": f"Employer {index}",
        }
        for index in range(13)
    ]
    roots = [
        _root(
            f"{section} content",
            overflowing if section == "experience" else None,
        )
        for section in PERSON_SECTIONS
    ]
    page.script("evaluate:root_content", *roots)
    page.script(
        "evaluate:profile_urn",
        "/messaging/compose/?recipient=ACoAA-policy",
    )
    page.declare_locator("main button", "show_more")
    page.declare_derived(
        "show_more",
        "filter:^Show (more|all)\\b/re.IGNORECASE|re.UNICODE",
        "show_more.filtered",
    )
    page.script("show_more.filtered.count", *([0] * 8))
    extractor = _extractor(page)
    callbacks = TraceCallbacks(recorder)
    async with boundaries(recorder, clock):
        with recorder.context("scrape_person"):
            result = await extractor.scrape_person(
                "ada-lovelace", set(PERSON_SECTIONS), callbacks=callbacks
            )
    page.assert_clean()
    return recorder.trace(
        {
            "method": "scrape_person",
            "arguments": {
                "username": "ada-lovelace",
                "requested": list(PERSON_SECTIONS),
            },
        },
        {
            "section_names": list(result["sections"]),
            # Keep the values, not only the keys: without them, serving every
            # section the first section's text leaves the trace unchanged.
            "sections": result["sections"],
            "references": result.get("references"),
            "profile_urn": result.get("profile_urn"),
        },
    )


async def _company_sections_scenario() -> dict[str, Any]:
    name = "scrape_company__all_sections"
    recorder = TraceRecorder(name, _COMMON_ALLOWED)
    clock = FakeClock(recorder)
    page = _page(recorder).script(
        "evaluate:root_content",
        *[_root(f"{section} content") for section in COMPANY_SECTIONS],
    )
    extractor = _extractor(page)
    callbacks = TraceCallbacks(recorder)
    async with boundaries(recorder, clock):
        with recorder.context("scrape_company"):
            result = await extractor.scrape_company(
                "analytical-engine", set(COMPANY_SECTIONS), callbacks=callbacks
            )
    page.assert_clean()
    return recorder.trace(
        {
            "method": "scrape_company",
            "arguments": {
                "company_name": "analytical-engine",
                "requested": list(COMPANY_SECTIONS),
            },
        },
        {
            "section_names": list(result["sections"]),
            "sections": result["sections"],
        },
    )


async def _job_search_scenario(route: str = "/jobs/search/") -> dict[str, Any]:
    is_alias = "search-results" in route
    name = "search_jobs__route_alias" if is_alias else "search_jobs__baseline"
    recorder = TraceRecorder(name, _COMMON_ALLOWED)
    clock = FakeClock(recorder)
    page = _page(recorder)
    page.goto_landings.append(f"https://www.linkedin.com{route}?keywords=python")
    ids = ["101", "102"] if is_alias else [str(101 + index) for index in range(16)]
    references = [
        {
            "href": "https://www.linkedin.com/jobs/view/101/",
            "text": "Senior policy engineer",
            "heading": "",
        },
        *[
            {
                "href": f"https://www.linkedin.com/company/company-{index}/",
                "text": f"Company {index}",
                "heading": "",
            }
            for index in range(20)
        ],
    ]
    page.script("evaluate:root_content", _root("Python jobs", references))
    page.script("evaluate:job_total_pages", None)
    page.script("evaluate:job_ids", {"ids": ids, "scoped": True})
    extractor = _extractor(page)
    async with boundaries(recorder, clock):
        with recorder.context("search_jobs", "search_results"):
            result = await extractor.search_jobs("python", max_pages=1)
    page.assert_clean()
    return recorder.trace(
        {"method": "search_jobs", "arguments": {"keywords": "python", "max_pages": 1}},
        {"job_ids": result["job_ids"], "references": result.get("references")},
    )


async def _job_search_upgrade_scenario() -> dict[str, Any]:
    recorder = TraceRecorder(
        "search_jobs__stopping_page_metadata_upgrade", _COMMON_ALLOWED
    )
    clock = FakeClock(recorder)
    page = _page(recorder)
    first = [
        {
            "href": "https://www.linkedin.com/jobs/view/101/",
            "text": "Job",
            "heading": "",
        }
    ]
    second = [
        {
            "href": "https://www.linkedin.com/jobs/view/101/",
            "text": "Senior policy engineer with richer stopping-page metadata",
            "heading": "",
        }
    ]
    page.script(
        "evaluate:root_content",
        _root("First page", first),
        _root("Stopping page", second),
    )
    page.script("evaluate:job_total_pages", None)
    page.script(
        "evaluate:job_ids",
        {"ids": ["101"], "scoped": True},
        {"ids": ["101"], "scoped": True},
    )
    extractor = _extractor(page)
    async with boundaries(recorder, clock):
        with recorder.context("search_jobs", "search_results"):
            result = await extractor.search_jobs("python", max_pages=2)
    page.assert_clean()
    return recorder.trace(
        {"method": "search_jobs", "arguments": {"keywords": "python", "max_pages": 2}},
        {"job_ids": result["job_ids"], "references": result.get("references")},
    )


async def _saved_jobs_scenario() -> dict[str, Any]:
    name = "get_saved_jobs__redirect_caps_and_upgrade"
    recorder = TraceRecorder(name, _COMMON_ALLOWED)
    clock = FakeClock(recorder)
    page = _page(recorder)
    page.goto_landings.append("https://www.linkedin.com/jobs-tracker/")
    first_references = [
        *[
            {
                "href": f"https://www.linkedin.com/jobs/view/{100 + index}/",
                "text": f"Job {100 + index}",
                "heading": "",
            }
            for index in range(12)
        ],
        {
            "href": "https://www.linkedin.com/company/cap-boundary/",
            "text": "Cap boundary company",
            "heading": "",
        },
        *[
            {
                "href": f"https://www.linkedin.com/jobs/view/{113 + index}/",
                "text": f"Job {113 + index}",
                "heading": "",
            }
            for index in range(7)
        ],
    ]
    second_references = [
        {
            "href": "https://www.linkedin.com/jobs/view/100/",
            "text": "Senior policy engineer with richer duplicate metadata",
            "heading": "",
        },
        *[
            {
                "href": f"https://www.linkedin.com/jobs/view/{112 + index}/",
                "text": f"Job {112 + index}",
                "heading": "",
            }
            for index in range(19)
        ],
    ]
    page.script(
        "evaluate:root_content",
        _root("Saved jobs page one", first_references),
        _root("Saved jobs page two", second_references),
    )
    page.script("evaluate:saved_job_total_pages", 2)
    page.script(
        "evaluate:job_ids",
        {"ids": [str(100 + index) for index in range(10)], "scoped": False},
        {"ids": [str(110 + index) for index in range(10)], "scoped": False},
    )
    extractor = _extractor(page)
    async with boundaries(recorder, clock):
        with recorder.context("get_saved_jobs", "saved_jobs"):
            result = await extractor.get_saved_jobs(max_pages=2)
    page.assert_clean()
    references = result.get("references", {}).get("saved_jobs", [])
    return recorder.trace(
        {"method": "get_saved_jobs", "arguments": {"max_pages": 2}},
        {
            "job_ids": result["job_ids"],
            "references": references,
            "reference_count": len(references),
        },
    )


async def _feed_stale_scenario() -> dict[str, Any]:
    name = "extract_feed__stale_stop"
    recorder = TraceRecorder(name, _COMMON_ALLOWED)
    clock = FakeClock(recorder)
    page = _page(recorder).script("evaluate:root_content", _root("Feed content"))
    extractor = _extractor(page)
    async with boundaries(recorder, clock):
        with recorder.context("extract_feed", "feed"):
            result = await extractor.extract_feed(num_posts=10)
    page.assert_clean()
    return recorder.trace(
        {"method": "extract_feed", "arguments": {"num_posts": 10}},
        {"text": result.text},
    )


async def _feed_response_scenario(*, body_failure: bool) -> dict[str, Any]:
    suffix = "body_failure" if body_failure else "body_success"
    recorder = TraceRecorder(
        f"extract_feed__{suffix}",
        _COMMON_ALLOWED | {"response.body.start", "response.body.finish"},
    )
    clock = FakeClock(recorder)
    page = _page(recorder).script("evaluate:root_content", _root("Feed content"))
    body: bytes | BaseException
    if body_failure:
        body = RuntimeError("response body unavailable")
    else:
        body = (
            b'{"postSlugUrl":"https://www.linkedin.com/posts/'
            b'policy-ugcPost-123-example"}'
        )
    response = ScriptedResponse(
        recorder,
        "https://www.linkedin.com/feed/",
        body,
    )
    page.script("mouse.wheel", lambda: page.emit("response", response))
    extractor = _extractor(page)
    async with boundaries(recorder, clock):
        with recorder.context("extract_feed", "feed"):
            result = await extractor.extract_feed(num_posts=1)
    page.assert_clean()
    return recorder.trace(
        {"method": "extract_feed", "arguments": {"num_posts": 1}},
        {"references": result.references, "text": result.text},
    )


async def _messaging_safety_scenario(confirm_send: bool) -> dict[str, Any]:
    suffix = "confirmed_append" if confirm_send else "confirmation_gate"
    name = f"send_message__{suffix}"
    recorder = TraceRecorder(name, _COMMON_ALLOWED)
    clock = FakeClock(recorder)
    page = _page(recorder)
    page.script("evaluate:profile_display_name", "Ada Lovelace")
    page.declare_locator(
        extractor_module._MESSAGING_RECIPIENT_PICKER_SELECTOR, "recipient_picker"
    )
    page.script("recipient_picker.count", 0)
    for index, selector in enumerate(
        extractor_module._MESSAGING_COMPOSE_FALLBACK_SELECTORS
    ):
        semantic_id = f"composer.{index}"
        page.declare_locator(selector, semantic_id)
        page.declare_derived(semantic_id, "last", f"{semantic_id}.last")
        counts = (1, 1) if index == 0 else (0,)
        page.script(f"{semantic_id}.count", *counts)
    page.script("evaluate:compose_matches_recipient", True)
    page.declare_locator(extractor_module._MESSAGING_CLOSE_SELECTOR, "message_close")
    page.script("message_close.count", 0)
    if confirm_send:
        page.script("evaluate:focus_message_composer", True)
        page.script("evaluate:click_send_button", True)
        page.script("wait_for_function:message_text_visible", None)
    extractor = _extractor(page)
    async with boundaries(recorder, clock):
        with recorder.context("send_message", "message"):
            result = await extractor.send_message(
                "ada-lovelace",
                "New text",
                confirm_send=confirm_send,
                profile_urn="ACoAA-policy",
            )
    page.assert_clean()
    return recorder.trace(
        {
            "method": "send_message",
            "arguments": {
                "linkedin_username": "ada-lovelace",
                "message": "New text",
                "confirm_send": confirm_send,
                "profile_urn": "ACoAA-policy",
                "composer_initial_text": "Existing text",
            },
        },
        result,
    )


async def _single_capture_facade_scenario(method: str) -> dict[str, Any]:
    name = f"{method}__baseline"
    recorder = TraceRecorder(name, _COMMON_ALLOWED)
    clock = FakeClock(recorder)
    page = _page(recorder).script("evaluate:root_content", _root("Result content"))
    extractor = _extractor(page)
    arguments: dict[str, Any]
    async with boundaries(recorder, clock):
        with recorder.context(method):
            if method == "get_company_employees":
                arguments = {"company_name": "analytical-engine", "keywords": "math"}
                result = await extractor.get_company_employees(**arguments)
            elif method == "scrape_job":
                arguments = {"job_id": "123"}
                result = await extractor.scrape_job(**arguments)
            elif method == "search_people":
                arguments = {"keywords": "analyst", "network": ["F"]}
                result = await extractor.search_people(**arguments)
            elif method == "search_companies":
                arguments = {"keywords": "engine"}
                result = await extractor.search_companies(**arguments)
            elif method == "search_posts":
                arguments = {"keywords": "mathematics", "max_pages": 2}
                result = await extractor.search_posts(**arguments)
            else:
                raise AssertionError(method)
    page.assert_clean()
    return recorder.trace(
        {"method": method, "arguments": arguments},
        {
            "url": result["url"],
            "section_names": list(result["sections"]),
        },
    )


async def _get_my_profile_scenario() -> dict[str, Any]:
    name = "get_my_profile__baseline"
    recorder = TraceRecorder(name, _COMMON_ALLOWED)
    clock = FakeClock(recorder)
    page = _page(recorder)
    page.goto_landings.append("https://www.linkedin.com/in/ada-lovelace/")
    page.script("evaluate:root_content", _root("Own profile"))
    page.script("evaluate:profile_urn", "/messaging/compose/?recipient=ACoAA-self")
    extractor = _extractor(page)
    async with boundaries(recorder, clock):
        with recorder.context("get_my_profile", "main_profile"):
            result = await extractor.get_my_profile()
    page.assert_clean()
    return recorder.trace(
        {"method": "get_my_profile", "arguments": {}},
        {"url": result["url"], "profile_urn": result.get("profile_urn")},
    )


async def _connect_scenario() -> dict[str, Any]:
    name = "connect_with_person__self_profile"
    recorder = TraceRecorder(name, _COMMON_ALLOWED)
    clock = FakeClock(recorder)
    page = _page(recorder)
    page.script("evaluate:root_content", _root("Own profile"))
    page.script("evaluate:profile_urn", None)
    page.script(
        "evaluate:connection_action_signals",
        {
            "hasInvite": False,
            "hasComposeInActionRoot": False,
            "hasEditIntro": True,
            "hasLabeledActionButton": True,
            "hasLabeledActionAnchor": False,
            "hasIncomingActionRow": False,
        },
    )
    extractor = _extractor(page)
    async with boundaries(recorder, clock):
        with recorder.context("connect_with_person", "main_profile"):
            result = await extractor.connect_with_person("ada-lovelace")
    page.assert_clean()
    return recorder.trace(
        {"method": "connect_with_person", "arguments": {"username": "ada-lovelace"}},
        {"status": result["status"]},
    )


async def _sidebar_scenario() -> dict[str, Any]:
    name = "get_sidebar_profiles__baseline"
    recorder = TraceRecorder(name, _COMMON_ALLOWED)
    clock = FakeClock(recorder)
    page = _page(recorder).script(
        "evaluate:sidebar_profiles", {"sections": {}, "showAllUrls": {}}
    )
    extractor = _extractor(page)
    async with boundaries(recorder, clock):
        with recorder.context("get_sidebar_profiles", "sidebar"):
            result = await extractor.get_sidebar_profiles("ada-lovelace")
    page.assert_clean()
    return recorder.trace(
        {"method": "get_sidebar_profiles", "arguments": {"username": "ada-lovelace"}},
        result,
    )


async def _conversation_scenario(method: str) -> dict[str, Any]:
    name = f"{method}__baseline"
    recorder = TraceRecorder(name, _COMMON_ALLOWED)
    clock = FakeClock(recorder)
    page = _page(recorder)
    page.script("evaluate:scroll_main_region", *([True] * 3))
    page.script("evaluate:root_content", _root("Conversation content"))
    page.script(
        "wait_for_selector:conversation_rows",
        PlaywrightTimeoutError("no scripted rows"),
    )
    extractor = _extractor(page)
    async with boundaries(recorder, clock):
        with recorder.context(method, "conversation"):
            if method == "get_inbox":
                arguments = {"limit": 10}
                result = await extractor.get_inbox(limit=10)
            elif method == "get_conversation":
                arguments = {"thread_id": "2-abc"}
                result = await extractor.get_conversation(thread_id="2-abc")
            elif method == "search_conversations":
                arguments = {"keywords": "engine", "limit": 10}
                result = await extractor.search_conversations("engine", limit=10)
            else:
                raise AssertionError(method)
    page.assert_clean()
    return recorder.trace(
        {"method": method, "arguments": arguments},
        {"url": result["url"], "section_names": list(result["sections"])},
    )


def _facade_contract_trace() -> dict[str, Any]:
    methods = {}
    for name in TOOL_FACADE_METHODS | COMPATIBILITY_METHODS:
        member = getattr(LinkedInExtractor, name)
        methods[name] = {
            "signature": str(inspect.signature(member)),
            "coroutine": inspect.iscoroutinefunction(member),
        }
    return {
        "schema_version": 1,
        "scenario": "facade_contract",
        "call": {"method": "LinkedInExtractor", "arguments": {"constructor": "Page"}},
        "events": [],
        "result": {
            "tool_methods": sorted(TOOL_FACADE_METHODS),
            "compatibility_methods": sorted(COMPATIBILITY_METHODS),
            "methods": methods,
        },
    }


TOOL_FACADE_METHODS = {
    "connect_with_person",
    "extract_feed",
    "extract_page",
    "get_company_employees",
    "get_conversation",
    "get_inbox",
    "get_my_profile",
    "get_saved_jobs",
    "get_sidebar_profiles",
    "scrape_company",
    "scrape_job",
    "scrape_person",
    "search_companies",
    "search_conversations",
    "search_jobs",
    "search_people",
    "search_posts",
    "send_message",
}
COMPATIBILITY_METHODS = {"get_page_text", "click_button_by_text"}


async def build_policy_traces() -> dict[str, dict[str, Any]]:
    traces = {
        "facade-contract.json": _facade_contract_trace(),
        "generic-ordinary.json": await _generic_capture_scenario(
            "extract_page__ordinary", "https://www.linkedin.com/in/ada-lovelace/"
        ),
        "generic-activity.json": await _generic_capture_scenario(
            "extract_page__activity",
            "https://www.linkedin.com/in/ada-lovelace/recent-activity/all/",
        ),
        "generic-search.json": await _generic_capture_scenario(
            "extract_page__search",
            "https://www.linkedin.com/search/results/people/?keywords=ada",
        ),
        "generic-company-people.json": await _generic_capture_scenario(
            "extract_page__company_people",
            "https://www.linkedin.com/company/analytical-engine/people/",
        ),
        "person-sections.json": await _person_sections_scenario(),
        "company-sections.json": await _company_sections_scenario(),
        "job-search.json": await _job_search_scenario(),
        "job-search-route-alias.json": await _job_search_scenario(
            "/jobs/search-results/"
        ),
        "job-search-metadata-upgrade.json": await _job_search_upgrade_scenario(),
        "saved-jobs.json": await _saved_jobs_scenario(),
        "feed-stale.json": await _feed_stale_scenario(),
        "feed-response-success.json": await _feed_response_scenario(body_failure=False),
        "feed-response-failure.json": await _feed_response_scenario(body_failure=True),
        "message-confirmation.json": await _messaging_safety_scenario(False),
        "message-append.json": await _messaging_safety_scenario(True),
        "connect.json": await _connect_scenario(),
        "get-my-profile.json": await _get_my_profile_scenario(),
        "sidebar-profiles.json": await _sidebar_scenario(),
        "company-employees.json": await _single_capture_facade_scenario(
            "get_company_employees"
        ),
        "scrape-job.json": await _single_capture_facade_scenario("scrape_job"),
        "search-people.json": await _single_capture_facade_scenario("search_people"),
        "search-companies.json": await _single_capture_facade_scenario(
            "search_companies"
        ),
        "search-posts.json": await _single_capture_facade_scenario("search_posts"),
        "inbox.json": await _conversation_scenario("get_inbox"),
        "conversation.json": await _conversation_scenario("get_conversation"),
        "search-conversations.json": await _conversation_scenario(
            "search_conversations"
        ),
    }
    return traces


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
