"""Canonical semantic scraping-policy scenarios."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from difflib import unified_diff
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import ast
import asyncio
import inspect
import json
import subprocess
import tomllib

from hashlib import sha256

from patchright.async_api import Page
from patchright.async_api import TimeoutError as PlaywrightTimeoutError

from linkedin_mcp_server.callbacks import ProgressCallback
from linkedin_mcp_server.scraping import capture as capture_module
from linkedin_mcp_server.scraping import extractor as extractor_module
from linkedin_mcp_server.scraping import navigation as navigation_module
from linkedin_mcp_server.scraping import session as session_module
from linkedin_mcp_server.scraping.extractor import LinkedInExtractor
from linkedin_mcp_server.scraping.fields import COMPANY_SECTIONS, PERSON_SECTIONS
from linkedin_mcp_server.server import create_mcp_server

from .support.policy_trace import (
    FakeClock,
    ScriptedPage,
    ScriptedResponse,
    TraceRecorder,
    bind_effective,
)


ROOT = Path(__file__).parents[2]
TRACE_ROOT = ROOT / "tests" / "fixtures" / "scraping-policy" / "v1"
PRODUCTION_BASELINE = "70e50ada68b9389f8d315df6ab1e56c08f6c985b"
BASELINE_PROVENANCE_SHA256 = (
    "a446e0152c4b6430c83f56b8f663baa9824c48803879baa822c840f5771e67e8"
)
_TOOL_SCHEMAS: dict[str, dict[str, Any]] | None = None

_COMMON_ALLOWED = {
    "boundary.auth",
    "boundary.auth_quick",
    "boundary.drain",
    "boundary.modal",
    "boundary.rate_limit",
    "boundary.scroll_body",
    "boundary.scroll_sidebar",
    "boundary.stabilize",
    "boundary.trace",
    "callback.complete",
    "callback.progress",
    "callback.start",
    "evaluate",
    "evaluate_handle",
    "handle.as_element",
    "handle.dispose",
    "handle.evaluate",
    "keyboard.press",
    "keyboard.type",
    "listener.add",
    "listener.emit",
    "listener.remove",
    "locator.click",
    "locator.count",
    "locator.create",
    "locator.derive",
    "locator.is_visible",
    "locator.scroll_into_view",
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
async def boundaries(
    recorder: TraceRecorder,
    clock: FakeClock,
    *,
    auth_result: str | None = None,
) -> AsyncIterator[None]:
    real_scroll_body = extractor_module.scroll_to_bottom
    real_scroll_sidebar = extractor_module.scroll_job_sidebar
    real_drain = extractor_module._drain_listener_tasks

    async def trace(_page: Any, label: str, *, extra: Any = None) -> None:
        recorder.record("boundary.trace", label=label, extra=extra)

    async def auth_quick(_page: Any) -> None:
        recorder.record("boundary.auth_quick", result=None)
        return None

    async def auth(_page: Any) -> str | None:
        recorder.record("boundary.auth", result=auth_result)
        return auth_result

    async def remember(_page: Any) -> bool:
        return False

    async def stabilize(description: str, _logger: Any) -> None:
        recorder.record(
            "boundary.stabilize",
            description=description,
            result=None,
        )

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
        patch.object(navigation_module, "record_page_trace", trace),
        patch.object(navigation_module, "detect_auth_barrier_quick", auth_quick),
        patch.object(navigation_module, "detect_auth_barrier", auth),
        patch.object(navigation_module, "resolve_remember_me_prompt", remember),
        patch.object(navigation_module, "stabilize_navigation", stabilize),
        # Both bindings of each shared boundary, because the workflows that
        # reach it are split across the modules mid-relocation: generic capture
        # goes through `ScrapingSession`, while the job, conversation and
        # messaging workflows still call the imported helper on the facade.
        # Patching one side only lets the real helper loose on a scripted page.
        patch.object(session_module, "detect_rate_limit", rate_limit),
        patch.object(extractor_module, "detect_rate_limit", rate_limit),
        patch.object(session_module, "handle_modal_close", modal),
        patch.object(extractor_module, "handle_modal_close", modal),
        patch.object(session_module, "scroll_to_bottom", scroll_body),
        patch.object(extractor_module, "scroll_to_bottom", scroll_body),
        patch.object(session_module, "scroll_job_sidebar", scroll_sidebar),
        patch.object(extractor_module, "scroll_job_sidebar", scroll_sidebar),
        patch.object(capture_module, "build_issue_diagnostics", diagnostics),
        patch.object(extractor_module, "build_issue_diagnostics", diagnostics),
        patch.object(extractor_module, "_drain_listener_tasks", drain),
        patch.object(session_module.asyncio, "sleep", clock.sleep),
        patch.object(session_module.time, "monotonic", clock.monotonic),
    ):
        yield


def _page(recorder: TraceRecorder, *, url: str = "about:blank") -> ScriptedPage:
    return ScriptedPage(recorder, url=url).script("evaluate:root_content")


def _extractor(page: ScriptedPage) -> LinkedInExtractor:
    return LinkedInExtractor(cast(Page, page))


def _complete_mapping_result(result: dict[str, Any], **derived: Any) -> dict[str, Any]:
    overlap = result.keys() & derived.keys()
    if overlap:
        raise AssertionError(
            f"derived result fields overlap raw result: {sorted(overlap)}"
        )
    return {**result, **derived}


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
    _script_profile_target(page)
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
        _complete_mapping_result(result, section_names=list(result["sections"])),
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
        _complete_mapping_result(result, section_names=list(result["sections"])),
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
        result,
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
        result,
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
        _complete_mapping_result(result, reference_count=len(references)),
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
        {"references": result.references, "text": result.text},
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


_MESSAGE_PROFILE_URL = "https://www.linkedin.com/in/ada-lovelace/"
_MESSAGE_COMPOSE_URL = (
    "https://www.linkedin.com/messaging/compose/"
    "?recipient=ACoAA-policy&profileUrn=urn%3Ali%3Afsd_profile%3AACoAA-policy"
)
_MESSAGE_ROUTE = "https://www.linkedin.com/messaging/thread/2-policy-thread==/"
_MESSAGE_TARGET = {
    "profilePath": "/in/ada-lovelace/",
    "profileUrn": "ACoAA-policy",
}
_VALID_COMPOSER = {
    "status": "valid",
    "active": False,
    "empty": True,
    "submitCount": 1,
    "submitUsable": True,
}


def _profile_target(
    status: str = "resolved", *, profile_urn: str = "ACoAA-policy"
) -> dict[str, Any]:
    if status == "resolved":
        compose_url = (
            _MESSAGE_COMPOSE_URL
            if profile_urn == "ACoAA-policy"
            else "https://www.linkedin.com/messaging/compose/?recipient=" + profile_urn
        )
        return {
            "status": "resolved",
            "pageUrl": _MESSAGE_PROFILE_URL,
            "displayName": "Ada Lovelace",
            "composeHrefs": [compose_url],
        }
    if status == "unavailable":
        return {"status": "unavailable", "pageUrl": _MESSAGE_PROFILE_URL}
    return {"status": "unresolved"}


def _script_profile_target(
    page: ScriptedPage,
    status: str = "resolved",
    *,
    profile_urn: str = "ACoAA-policy",
) -> None:
    page.script(
        "wait_for_function:profile_message_target_ready",
        None
        if status == "resolved"
        else PlaywrightTimeoutError("profile Message action did not resolve"),
    )
    page.script(
        "evaluate:profile_message_target",
        _profile_target(status, profile_urn=profile_urn),
    )


def _script_message_surface(
    page: ScriptedPage,
    *,
    states: tuple[dict[str, Any], ...],
    route: str = _MESSAGE_ROUTE,
) -> None:
    page.goto_landings.append(_MESSAGE_PROFILE_URL)
    page.goto_landings.append(route)
    _script_profile_target(page)
    page.script("wait_for_function:message_composer_ready", None)
    page.script("evaluate:message_composer_state", *states)


def _script_message_owner(page: ScriptedPage, *, write: str = "written") -> None:
    page.script("evaluate_handle:message_composer_owner", True)
    page.script("handle-1.evaluate:message_composer_write", write)
    page.script("handle-1.evaluate:message_composer_dispose", None)


def _script_confirmation_cleanup(page: ScriptedPage) -> None:
    page.script("evaluate:message_confirmation_dispose", None)


def _script_owned_text_cleanup(page: ScriptedPage, *, removed: bool) -> None:
    page.script("handle-1.evaluate:message_composer_cleanup", removed)


async def _message_target_scenario(status: str) -> dict[str, Any]:
    recorder = TraceRecorder(f"send_message__target_{status}", _COMMON_ALLOWED)
    clock = FakeClock(recorder)
    page = _page(recorder)
    page.goto_landings.append(_MESSAGE_PROFILE_URL)
    _script_profile_target(page, status)
    extractor = _extractor(page)
    async with boundaries(recorder, clock):
        with recorder.context("send_message", "message"):
            result = await extractor.send_message(
                "ada-lovelace",
                "New text",
                confirm_send=True,
                profile_urn="ACoAA-policy",
            )
    page.assert_clean()
    return recorder.trace(
        {
            "method": "send_message",
            "arguments": {"target_resolution": status, "confirm_send": True},
        },
        result,
    )


async def _messaging_dry_run_scenario() -> dict[str, Any]:
    recorder = TraceRecorder("send_message__dry_run", _COMMON_ALLOWED)
    clock = FakeClock(recorder)
    page = _page(recorder)
    _script_message_surface(page, states=(_VALID_COMPOSER,))
    extractor = _extractor(page)
    async with boundaries(recorder, clock):
        with recorder.context("send_message", "message"):
            result = await extractor.send_message(
                "ada-lovelace",
                "New text",
                confirm_send=False,
                profile_urn="ACoAA-policy",
            )
    page.assert_clean()
    return recorder.trace(
        {
            "method": "send_message",
            "arguments": {
                "linkedin_username": "ada-lovelace",
                "message": "New text",
                "confirm_send": False,
                "profile_urn": "ACoAA-policy",
            },
        },
        result,
    )


async def _occupied_message_scenario(*, restored_during_write: bool) -> dict[str, Any]:
    suffix = "restored_during_write" if restored_during_write else "existing_draft"
    recorder = TraceRecorder(f"send_message__{suffix}", _COMMON_ALLOWED)
    clock = FakeClock(recorder)
    page = _page(recorder)
    second_state = (
        _VALID_COMPOSER
        if restored_during_write
        else {**_VALID_COMPOSER, "empty": False}
    )
    _script_message_surface(page, states=(_VALID_COMPOSER, second_state))
    if restored_during_write:
        _script_message_owner(page, write="occupied")
        _script_owned_text_cleanup(page, removed=False)
    extractor = _extractor(page)
    async with boundaries(recorder, clock):
        with recorder.context("send_message", "message"):
            result = await extractor.send_message(
                "ada-lovelace", "New text", confirm_send=True
            )
    page.assert_clean()
    return recorder.trace(
        {
            "method": "send_message",
            "arguments": {
                "confirm_send": True,
                "composer_occupied": suffix,
            },
        },
        result,
    )


async def _messaging_submission_scenario(outcome: str) -> dict[str, Any]:
    recorder = TraceRecorder(f"send_message__{outcome}", _COMMON_ALLOWED)
    clock = FakeClock(recorder)
    page = _page(recorder)
    _script_message_surface(page, states=(_VALID_COMPOSER, _VALID_COMPOSER))
    _script_message_owner(page)

    if outcome == "pre_submit_cleanup":
        page.script("handle-1.evaluate:message_submit_ready", "invalid")
        _script_owned_text_cleanup(page, removed=True)
    else:
        ready_states = ("disabled", "ready") if outcome == "sent" else ("ready",)
        page.script("handle-1.evaluate:message_submit_ready", *ready_states)
        page.script("evaluate:message_confirmation_prepare", "confirmation-1")
        if outcome == "submission_rejected":
            page.script("handle-1.evaluate:message_submit", "invalid")
            _script_confirmation_cleanup(page)
            _script_owned_text_cleanup(page, removed=True)
        elif outcome == "submission_interrupted":
            page.script(
                "handle-1.evaluate:message_submit",
                RuntimeError("submission round trip interrupted"),
            )
            _script_confirmation_cleanup(page)
        else:
            page.script("handle-1.evaluate:message_submit", "clicked")
            page.script(
                "wait_for_function:message_confirmation_ready",
                None
                if outcome == "sent"
                else PlaywrightTimeoutError("same-node transition not observed"),
            )
            _script_confirmation_cleanup(page)

    extractor = _extractor(page)
    async with boundaries(recorder, clock):
        with recorder.context("send_message", "message"):
            result = await extractor.send_message(
                "ada-lovelace", "New text", confirm_send=True
            )
    page.assert_clean()
    return recorder.trace(
        {
            "method": "send_message",
            "arguments": {"confirm_send": True, "submission_outcome": outcome},
        },
        result,
    )


async def _messaging_cancellation_scenario() -> dict[str, Any]:
    recorder = TraceRecorder("send_message__confirmation_cancelled", _COMMON_ALLOWED)
    clock = FakeClock(recorder)
    page = _page(recorder)
    _script_message_surface(page, states=(_VALID_COMPOSER, _VALID_COMPOSER))
    _script_message_owner(page)
    page.script("handle-1.evaluate:message_submit_ready", "ready")
    page.script("evaluate:message_confirmation_prepare", "confirmation-1")
    page.script("handle-1.evaluate:message_submit", "clicked")
    page.script(
        "wait_for_function:message_confirmation_ready", asyncio.CancelledError()
    )
    _script_confirmation_cleanup(page)
    extractor = _extractor(page)
    async with boundaries(recorder, clock):
        with recorder.context("send_message", "message"):
            try:
                await extractor.send_message(
                    "ada-lovelace", "New text", confirm_send=True
                )
            except asyncio.CancelledError:
                result = {"raised": "CancelledError"}
            else:
                raise AssertionError("message confirmation cancellation was swallowed")
    page.assert_clean()
    return recorder.trace(
        {
            "method": "send_message",
            "arguments": {"confirm_send": True, "cancelled_during": "confirmation"},
        },
        result,
    )


async def _invalid_message_scenario(message: str, label: str) -> dict[str, Any]:
    recorder = TraceRecorder(f"send_message__invalid_{label}", _COMMON_ALLOWED)
    clock = FakeClock(recorder)
    page = _page(recorder)
    extractor = _extractor(page)
    async with boundaries(recorder, clock):
        with recorder.context("send_message", "message"):
            result = await extractor.send_message(
                "ada-lovelace",
                message,
                confirm_send=True,
                profile_urn="ACoAA-policy",
            )
    page.assert_clean()
    return recorder.trace(
        {
            "method": "send_message",
            "arguments": {
                "linkedin_username": "ada-lovelace",
                "message_case": label,
                "confirm_send": True,
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
        _complete_mapping_result(result, section_names=list(result["sections"])),
    )


async def _single_capture_error_scenario() -> dict[str, Any]:
    recorder = TraceRecorder("scrape_job__capture_error", _COMMON_ALLOWED)
    clock = FakeClock(recorder)
    page = _page(recorder).script(
        "evaluate:root_content", RuntimeError("synthetic capture failure")
    )
    extractor = _extractor(page)
    arguments = {"job_id": "123"}
    async with boundaries(recorder, clock):
        with recorder.context("scrape_job"):
            result = await extractor.scrape_job(**arguments)
    page.assert_clean()
    return recorder.trace(
        {"method": "scrape_job", "arguments": arguments},
        _complete_mapping_result(result, section_names=list(result["sections"])),
    )


async def _get_my_profile_scenario() -> dict[str, Any]:
    name = "get_my_profile__baseline"
    recorder = TraceRecorder(name, _COMMON_ALLOWED)
    clock = FakeClock(recorder)
    page = _page(recorder)
    page.goto_landings.append("https://www.linkedin.com/in/ada-lovelace/")
    page.script("evaluate:root_content", _root("Own profile"))
    _script_profile_target(page, profile_urn="ACoAA-self")
    extractor = _extractor(page)
    async with boundaries(recorder, clock):
        with recorder.context("get_my_profile", "main_profile"):
            result = await extractor.get_my_profile()
    page.assert_clean()
    return recorder.trace(
        {"method": "get_my_profile", "arguments": {}},
        result,
    )


async def _connect_scenario() -> dict[str, Any]:
    name = "connect_with_person__self_profile"
    recorder = TraceRecorder(name, _COMMON_ALLOWED)
    clock = FakeClock(recorder)
    page = _page(recorder)
    page.script("evaluate:root_content", _root("Own profile"))
    _script_profile_target(page, "unavailable")
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
        result,
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
    if method != "search_conversations":
        scrolls = 3 if method == "get_conversation" else 1
        page.script("evaluate:scroll_main_region", *([True] * scrolls))
    page.script("evaluate:root_content", _root("Conversation content"))
    if method != "get_conversation":
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
        _complete_mapping_result(result, section_names=list(result["sections"])),
    )


def _baseline_file(path: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(ROOT), "show", f"{PRODUCTION_BASELINE}:{path}"],
        check=True,
        capture_output=True,
    ).stdout


def _generated_baseline_provenance_trace() -> dict[str, Any]:
    extractor_source = _baseline_file(
        "linkedin_mcp_server/scraping/extractor.py"
    ).decode("utf-8")
    tree = ast.parse(extractor_source)
    facade = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "LinkedInExtractor"
    )
    methods = [
        node
        for node in facade.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    public_coroutines = [
        node
        for node in methods
        if isinstance(node, ast.AsyncFunctionDef) and not node.name.startswith("_")
    ]
    mutable_attributes = sorted(
        {
            target.attr
            for node in ast.walk(facade)
            if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign))
            for target in (
                node.targets if isinstance(node, ast.Assign) else [node.target]
            )
            if isinstance(target, ast.Attribute)
            and isinstance(target.value, ast.Name)
            and target.value.id == "self"
        }
    )

    lock_bytes = _baseline_file("uv.lock")
    lock = tomllib.loads(lock_bytes.decode("utf-8"))
    dependencies: dict[str, set[str]] = {}
    for package in lock["package"]:
        version = package.get("version")
        if isinstance(version, str):
            dependencies.setdefault(package["name"], set()).add(version)

    result = {
        "production_baseline": PRODUCTION_BASELINE,
        "python_version": _baseline_file(".python-version").decode("utf-8").strip(),
        "uv_lock_sha256": sha256(lock_bytes).hexdigest(),
        "resolved_dependencies": {
            name: sorted(versions) for name, versions in sorted(dependencies.items())
        },
        "extractor_inventory": {
            "line_count": len(extractor_source.splitlines()),
            "method_count": len(methods),
            "public_coroutine_count": len(public_coroutines),
            "public_coroutines": sorted(node.name for node in public_coroutines),
            "mutable_instance_attributes": mutable_attributes,
        },
    }
    return {
        "schema_version": 1,
        "scenario": "baseline_provenance",
        "call": {
            "method": "git_show",
            "arguments": {"production_baseline": PRODUCTION_BASELINE},
        },
        "events": [],
        "result": result,
    }


def _baseline_object_exists() -> bool:
    result = subprocess.run(
        [
            "git",
            "-C",
            str(ROOT),
            "cat-file",
            "-e",
            f"{PRODUCTION_BASELINE}^{{commit}}",
        ],
        check=False,
        capture_output=True,
    )
    return result.returncode == 0


def _verify_baseline_provenance_bytes(raw: bytes) -> None:
    actual = sha256(raw).hexdigest()
    if actual != BASELINE_PROVENANCE_SHA256:
        raise AssertionError(
            "canonical baseline provenance hash mismatch: "
            f"expected {BASELINE_PROVENANCE_SHA256}, got {actual}"
        )


def _baseline_provenance_trace() -> dict[str, Any]:
    if _baseline_object_exists():
        trace = _generated_baseline_provenance_trace()
        _verify_baseline_provenance_bytes(canonical_json(trace).encode("utf-8"))
        return trace

    raw = (TRACE_ROOT / "baseline-provenance.json").read_bytes()
    _verify_baseline_provenance_bytes(raw)
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise AssertionError("canonical baseline provenance must be a JSON object")
    return value


async def _facade_contract_trace() -> dict[str, Any]:
    global _TOOL_SCHEMAS

    methods = {}
    for name in TOOL_FACADE_METHODS | COMPATIBILITY_METHODS:
        member = getattr(LinkedInExtractor, name)
        methods[name] = {
            "signature": str(inspect.signature(member)),
            "coroutine": inspect.iscoroutinefunction(member),
        }
    if _TOOL_SCHEMAS is None:
        tools = await create_mcp_server().list_tools()
        _TOOL_SCHEMAS = {
            tool.name: {
                "input": tool.parameters,
                "output": tool.output_schema,
            }
            for tool in sorted(tools, key=lambda item: item.name)
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
            "tool_schemas": _TOOL_SCHEMAS,
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
        "baseline-provenance.json": _baseline_provenance_trace(),
        "facade-contract.json": await _facade_contract_trace(),
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
        "message-target-unavailable.json": await _message_target_scenario(
            "unavailable"
        ),
        "message-target-unresolved.json": await _message_target_scenario("unresolved"),
        "message-dry-run.json": await _messaging_dry_run_scenario(),
        "message-composer-occupied.json": await _occupied_message_scenario(
            restored_during_write=False
        ),
        "message-composer-restored.json": await _occupied_message_scenario(
            restored_during_write=True
        ),
        "message-pre-submit-cleanup.json": await _messaging_submission_scenario(
            "pre_submit_cleanup"
        ),
        "message-submit-rejected.json": await _messaging_submission_scenario(
            "submission_rejected"
        ),
        "message-submit-interrupted.json": await _messaging_submission_scenario(
            "submission_interrupted"
        ),
        "message-unconfirmed.json": await _messaging_submission_scenario("unconfirmed"),
        "message-sent.json": await _messaging_submission_scenario("sent"),
        "message-cancelled.json": await _messaging_cancellation_scenario(),
        "message-blank.json": await _invalid_message_scenario("   ", "blank"),
        "message-c0.json": await _invalid_message_scenario("line\nbreak", "c0"),
        "message-del.json": await _invalid_message_scenario("text\x7f", "del"),
        "connect.json": await _connect_scenario(),
        "get-my-profile.json": await _get_my_profile_scenario(),
        "sidebar-profiles.json": await _sidebar_scenario(),
        "company-employees.json": await _single_capture_facade_scenario(
            "get_company_employees"
        ),
        "scrape-job.json": await _single_capture_facade_scenario("scrape_job"),
        "scrape-job-error.json": await _single_capture_error_scenario(),
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


def policy_trace_diff(
    generated: dict[str, dict[str, Any]], trace_root: Path = TRACE_ROOT
) -> str:
    """Return one deterministic unified comparison against canonical traces."""

    generated_names = set(generated)
    fixture_names = {path.name for path in trace_root.glob("*.json")}
    chunks = [
        f"missing canonical trace: {trace_root / name}\n"
        for name in sorted(generated_names - fixture_names)
    ]
    chunks.extend(
        f"unexpected canonical trace: {trace_root / name}\n"
        for name in sorted(fixture_names - generated_names)
    )
    for name in sorted(generated_names & fixture_names):
        path = trace_root / name
        expected = path.read_text(encoding="utf-8")
        actual = canonical_json(generated[name])
        chunks.extend(
            unified_diff(
                expected.splitlines(keepends=True),
                actual.splitlines(keepends=True),
                fromfile=str(path),
                tofile=f"generated/{name}",
            )
        )
    return "".join(chunks)
