"""Canonical semantic trace checks for extractor policy."""

from __future__ import annotations

from functools import wraps
from pathlib import Path
from typing import Any
from unittest.mock import patch

import json
import logging
import subprocess
import sys

import pytest

from linkedin_mcp_server.core.exceptions import AuthenticationError
from linkedin_mcp_server.scraping.extractor import LinkedInExtractor
from linkedin_mcp_server.scraping.fields import COMPANY_SECTIONS, PERSON_SECTIONS

from . import policy_scenarios
from .policy_scenarios import (
    TOOL_FACADE_METHODS,
    TRACE_ROOT,
    _complete_mapping_result,
    _extractor,
    boundaries,
    build_policy_traces,
    canonical_json,
    policy_trace_diff,
)
from .support.policy_trace import FakeClock, ScriptedPage, TraceRecorder


ROOT = Path(__file__).resolve().parents[2]
CHECKER = ROOT / "scripts" / "check_scraping_policy_traces.py"


def _operation_positions(trace: dict[str, Any]) -> dict[str, list[int]]:
    positions: dict[str, list[int]] = {}
    for index, event in enumerate(trace["events"]):
        operation = event.get("operation", event["kind"])
        positions.setdefault(operation, []).append(index)
    return positions


async def test_generated_traces_match_every_canonical_fixture():
    generated = await build_policy_traces()

    assert policy_trace_diff(generated) == ""


def test_complete_result_rejects_derived_field_collisions():
    with pytest.raises(AssertionError, match="overlap raw result"):
        _complete_mapping_result({"section_names": []}, section_names=[])


async def test_navigation_boundaries_keep_full_auth_and_stabilization_order():
    trace = (await build_policy_traces())["job-search-route-alias.json"]
    positions = _operation_positions(trace)

    assert (
        positions["boundary.stabilize"][0]
        < positions["boundary.auth_quick"][0]
        < positions["boundary.auth"][0]
        < positions["root_content"][0]
    )
    assert trace["events"][positions["boundary.stabilize"][0]] == {
        "kind": "boundary.stabilize",
        "call": "search_jobs",
        "section": "search_results",
        "description": "goto https://www.linkedin.com/jobs/search/?keywords=python",
        "result": None,
    }
    assert trace["events"][positions["boundary.auth"][0]]["result"] is None


async def test_stabilization_trace_ignores_physical_logger_identity():
    recorder = TraceRecorder("stabilize-logger", {"boundary.stabilize"})
    clock = FakeClock(recorder)

    relocated_logger = logging.getLogger("linkedin_mcp_server.scraping.navigation")

    async with boundaries(recorder, clock):
        await policy_scenarios.navigation_module.stabilize_navigation(
            "logical navigation", relocated_logger
        )

    assert recorder.events == [
        {
            "kind": "boundary.stabilize",
            "description": "logical navigation",
            "result": None,
        }
    ]


async def test_full_auth_boundary_propagates_a_detected_barrier():
    recorder = TraceRecorder("full-auth-barrier", {"boundary.auth"})
    clock = FakeClock(recorder)
    page = ScriptedPage(recorder)
    extractor = _extractor(page)

    async with boundaries(recorder, clock, auth_result="account picker"):
        with pytest.raises(AuthenticationError, match="interactive re-authentication"):
            await extractor._navigator._raise_if_auth_barrier(
                "https://www.linkedin.com/jobs/search/"
            )

    assert recorder.events == [{"kind": "boundary.auth", "result": "account picker"}]


async def test_trace_comparison_reports_a_unified_diff():
    generated = await build_policy_traces()
    generated["connect.json"]["result"]["status"] = "mutated"

    difference = policy_trace_diff(generated)

    assert f"--- {TRACE_ROOT / 'connect.json'}" in difference
    assert "+++ generated/connect.json" in difference
    assert '-    "status":' in difference
    assert '+    "status": "mutated"' in difference


def test_canonical_fixtures_are_portable_deterministic_json():
    for path in sorted(TRACE_ROOT.glob("*.json")):
        raw = path.read_bytes()
        decoded = raw.decode("utf-8")
        value = json.loads(decoded)

        assert raw.endswith(b"\n")
        assert decoded == canonical_json(value)
        assert "/Users/" not in decoded
        assert '"timestamp"' not in decoded
        assert '"seq"' not in decoded


async def test_trace_set_exercises_every_tool_facing_facade_method():
    traces = await build_policy_traces()
    called = {
        trace["call"]["method"]
        for trace in traces.values()
        if trace["call"]["method"] != "LinkedInExtractor"
    }

    assert called == TOOL_FACADE_METHODS


async def test_facade_results_keep_raw_values_and_optional_key_shape():
    traces = await build_policy_traces()
    company = traces["search-companies.json"]["result"]
    person = traces["person-sections.json"]["result"]
    assert company["sections"] == {"search_results": "Result content"}
    assert "references" not in company
    assert "section_errors" not in company
    assert person["references"]["experience"][0]["text"] == "Employer 0"


async def test_scrape_job_traces_keep_success_and_error_results_separate():
    traces = await build_policy_traces()
    successful_job = traces["scrape-job.json"]["result"]
    failed_job = traces["scrape-job-error.json"]["result"]

    assert successful_job["sections"] == {"job_posting": "Result content"}
    assert successful_job["section_names"] == ["job_posting"]
    assert "section_errors" not in successful_job
    assert failed_job["sections"] == {}
    assert failed_job["section_names"] == []
    assert failed_job["section_errors"] == {
        "job_posting": {
            "context": "extract_page",
            "error_message": "synthetic capture failure",
            "error_type": "RuntimeError",
        }
    }


async def test_facade_trace_detects_section_text_corruption():
    original = LinkedInExtractor.search_companies

    @wraps(original)
    async def corrupt_sections(self: Any, *args: Any, **kwargs: Any) -> dict[str, Any]:
        result = await original(self, *args, **kwargs)
        result["sections"] = {name: "corrupted text" for name in result["sections"]}
        return result

    with patch.object(LinkedInExtractor, "search_companies", corrupt_sections):
        mutated = await build_policy_traces()

    assert "corrupted text" in policy_trace_diff(mutated)


async def test_facade_trace_detects_lost_references():
    original = LinkedInExtractor.scrape_person
    removed: list[dict[str, Any]] = []

    @wraps(original)
    async def drop_references(self: Any, *args: Any, **kwargs: Any) -> dict[str, Any]:
        result = await original(self, *args, **kwargs)
        references = result.pop("references", None)
        if references:
            removed.append(references)
        return result

    with patch.object(LinkedInExtractor, "scrape_person", drop_references):
        mutated = await build_policy_traces()

    assert removed
    assert '-    "references": {' in policy_trace_diff(mutated)


async def test_facade_trace_detects_optional_key_drift():
    original = LinkedInExtractor.search_companies

    @wraps(original)
    async def add_optional_key(self: Any, *args: Any, **kwargs: Any) -> dict[str, Any]:
        result = await original(self, *args, **kwargs)
        result["section_errors"] = {}
        return result

    with patch.object(LinkedInExtractor, "search_companies", add_optional_key):
        mutated = await build_policy_traces()

    assert '+    "section_errors": {}' in policy_trace_diff(mutated)


async def test_section_navigation_and_callbacks_remain_one_to_one():
    traces = await build_policy_traces()
    person = traces["person-sections.json"]
    company = traces["company-sections.json"]

    assert person["result"]["section_names"] == list(PERSON_SECTIONS)
    assert company["result"]["section_names"] == list(COMPANY_SECTIONS)
    assert sum(e["kind"] == "navigate" for e in person["events"]) == len(
        PERSON_SECTIONS
    )
    assert sum(e["kind"] == "navigate" for e in company["events"]) == len(
        COMPANY_SECTIONS
    )
    assert sum(e["kind"] == "callback.start" for e in person["events"]) == 1
    assert sum(e["kind"] == "callback.progress" for e in person["events"]) == len(
        PERSON_SECTIONS
    )
    assert sum(e["kind"] == "callback.complete" for e in person["events"]) == 1


async def test_job_capture_precedes_validation_dependent_reads():
    trace = (await build_policy_traces())["job-search.json"]
    positions = _operation_positions(trace)

    root_read = positions["root_content"][0]
    post_capture_identity = positions["document_origin"][1]
    total_pages = positions["job_total_pages"][0]
    job_ids = positions["job_ids"][0]

    assert root_read < post_capture_identity < total_pages < job_ids
    expected_ids = [str(101 + index) for index in range(16)]
    references = trace["result"]["references"]["search_results"]
    assert trace["result"]["job_ids"] == expected_ids
    assert [reference["url"] for reference in references] == [
        f"/jobs/view/{job_id}/" for job_id in expected_ids
    ]
    assert references[0]["text"] == "Senior policy engineer"


async def test_job_reference_caps_fallbacks_and_stopping_page_upgrade():
    traces = await build_policy_traces()
    alias = traces["job-search-route-alias.json"]["result"]
    alias_references = alias["references"]["search_results"]
    upgraded = traces["job-search-metadata-upgrade.json"]["result"]

    assert alias["job_ids"] == ["101", "102"]
    assert len(alias_references) == 15
    assert sum(ref["kind"] == "job" for ref in alias_references) == 2
    assert sum(ref["kind"] != "job" for ref in alias_references) == 13
    assert upgraded["job_ids"] == ["101"]
    assert upgraded["references"]["search_results"] == [
        {
            "kind": "job",
            "url": "/jobs/view/101/",
            "text": "Senior policy engineer with richer stopping-page metadata",
            "context": "job result",
        }
    ]


async def test_saved_jobs_and_write_gate_keep_their_caps_and_ordering():
    traces = await build_policy_traces()
    saved = traces["saved-jobs.json"]
    confirmation = traces["message-confirmation.json"]
    sent = traces["message-append.json"]

    assert len(saved["result"]["job_ids"]) == 20
    assert saved["result"]["reference_count"] == 15
    assert saved["result"]["references"]["saved_jobs"][0]["text"] == (
        "Senior policy engineer with richer duplicate metadata"
    )
    assert not any(e["kind"] == "keyboard.type" for e in confirmation["events"])
    assert confirmation["result"]["status"] == "confirmation_required"

    type_index = next(
        index
        for index, event in enumerate(sent["events"])
        if event["kind"] == "keyboard.type"
    )
    send_index = next(
        index
        for index, event in enumerate(sent["events"])
        if event.get("operation") == "click_send_button"
    )
    assert type_index < send_index
    typed = next(e for e in sent["events"] if e["kind"] == "keyboard.type")
    assert typed["text"] == "New text"
    assert sent["result"]["status"] == "sent"


async def test_message_delivery_evidence_is_ordered_and_gated():
    traces = await build_policy_traces()
    sent = traces["message-append.json"]
    unconfirmed = traces["message-send-unavailable.json"]
    blank = traces["message-blank.json"]

    # Delivery is proven by a count that grew, so the baseline has to be taken
    # after the text is in the composer and before anything tries to send it.
    positions = _operation_positions(sent)
    assert positions.get("message_occurrences"), "no baseline occurrence count"
    assert (
        positions["keyboard.type"][0]
        < positions["message_occurrences"][0]
        < positions["click_send_button"][0]
    )
    confirmations = [
        event
        for event in sent["events"]
        if event.get("operation") == "message_occurrences_increased"
    ]
    assert [event["kind"] for event in confirmations] == ["wait_for_function"]
    assert confirmations[0]["arg"] == {"expected": "New text", "previous": 0}

    # An unconfirmed send reports failure and closes the draft it opened,
    # rather than leaving the composer standing with unsent text in it.
    assert unconfirmed["result"]["status"] == "send_unavailable"
    assert unconfirmed["result"]["sent"] is False
    unconfirmed_positions = _operation_positions(unconfirmed)
    assert unconfirmed_positions.get("keyboard.type"), "no send was ever attempted"
    dismissals = [
        index
        for index, event in enumerate(unconfirmed["events"])
        if event["kind"] == "locator.click"
        and event["locator"] == "message_close.first"
    ]
    assert len(dismissals) == 1, "the failed draft was not dismissed"
    assert unconfirmed_positions["message_occurrences_increased"][0] < dismissals[0]

    # The blank-message guard returns before the browser is touched at all.
    assert blank["result"]["status"] == "message_unavailable"
    assert blank["result"]["sent"] is False
    assert not any(e["kind"] == "navigate" for e in blank["events"])
    assert blank["events"] == []


async def test_message_recipient_picker_selects_before_confirmation_gate():
    selected = await policy_scenarios._recipient_picker_scenario(
        recipient_selected=True
    )
    failed = await policy_scenarios._recipient_picker_scenario(recipient_selected=False)

    positions = _operation_positions(selected)
    picker_wait = next(
        index
        for index, event in enumerate(selected["events"])
        if event["kind"] == "locator.wait_for"
        and event["locator"] == "recipient_picker.first"
    )
    composer_counts = [
        index
        for index, event in enumerate(selected["events"])
        if event["kind"] == "locator.count" and event["locator"] == "composer.0"
    ]

    assert len(composer_counts) == 2
    assert (
        picker_wait
        < positions["select_message_recipient"][0]
        < composer_counts[0]
        < composer_counts[1]
        < positions["compose_matches_recipient"][0]
    )
    selection = selected["events"][positions["select_message_recipient"][0]]
    assert selection["arg"] == {"candidates": ["Ada Lovelace", "ada-lovelace"]}
    assert selected["result"]["status"] == "confirmation_required"
    assert selected["result"]["recipient_selected"] is True
    assert not any(event["kind"] == "keyboard.type" for event in selected["events"])

    assert failed["result"]["status"] == "recipient_resolution_failed"
    assert failed["result"]["recipient_selected"] is False
    assert not any(
        event.get("operation") == "compose_matches_recipient"
        for event in failed["events"]
    )


@pytest.mark.parametrize(
    "fallback_index",
    [
        pytest.param(1, id="second-selector"),
        pytest.param(2, id="third-selector"),
    ],
)
async def test_message_composer_reaches_each_later_fallback(fallback_index: int):
    trace = await policy_scenarios._messaging_compose_fallback_scenario(fallback_index)
    expected_cycle = [f"composer.{index}" for index in range(fallback_index + 1)]
    composer_counts = [
        event["locator"]
        for event in trace["events"]
        if event["kind"] == "locator.count" and event["locator"].startswith("composer.")
    ]
    waits = [
        event["locator"]
        for event in trace["events"]
        if event["kind"] == "locator.wait_for"
        and event["locator"].startswith("composer.")
    ]
    created_selectors = [
        event["selector"]
        for event in trace["events"]
        if event["kind"] == "locator.create"
        and event["locator"].startswith("composer.")
    ]

    assert composer_counts == expected_cycle * 2
    assert waits == [
        f"composer.{index}.last" for _ in range(2) for index in range(fallback_index)
    ]
    assert created_selectors == [
        policy_scenarios._EXPECTED_MESSAGE_COMPOSE_SELECTORS[index]
        for _ in range(2)
        for index in range(fallback_index + 1)
    ]
    assert composer_counts.count(f"composer.{fallback_index}") == 2
    assert trace["result"]["status"] == "confirmation_required"
    assert trace["result"]["recipient_selected"] is True
    assert not any(event["kind"] == "keyboard.type" for event in trace["events"])


async def test_feed_stale_stop_and_listener_cleanup_are_bounded():
    feed = (await build_policy_traces())["feed-stale.json"]
    events = feed["events"]

    assert sum(e["kind"] == "mouse.wheel" for e in events) == 3
    assert [e["callback_id"] for e in events if e["kind"] == "listener.add"] == [
        "callback-1",
        "callback-2",
    ]
    assert [e["callback_id"] for e in events if e["kind"] == "listener.remove"] == [
        "callback-2",
        "callback-1",
    ]


async def test_feed_response_tasks_cover_success_and_body_failure():
    traces = await build_policy_traces()
    success = traces["feed-response-success.json"]
    failure = traces["feed-response-failure.json"]

    assert success["result"]["references"] == [
        {
            "kind": "feed_post",
            "url": "/posts/policy-ugcPost-123-example",
            "context": "feed",
        }
    ]
    assert sum(e["kind"] == "mouse.wheel" for e in success["events"]) == 1
    assert failure["result"]["references"] == []
    assert sum(e["kind"] == "mouse.wheel" for e in failure["events"]) == 3
    for trace in (success, failure):
        assert [
            e["kind"] for e in trace["events"] if e["kind"].startswith("response.body.")
        ] == ["response.body.start", "response.body.finish"]
        assert any(e["kind"] == "listener.emit" for e in trace["events"])


def test_trace_checker_generates_only_outside_canonical_fixture_tree(tmp_path):
    output = tmp_path / "candidate-traces"
    result = subprocess.run(
        [sys.executable, str(CHECKER), "--output", str(output)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert {path.name for path in output.glob("*.json")} == {
        path.name for path in TRACE_ROOT.glob("*.json")
    }

    result = subprocess.run(
        [sys.executable, str(CHECKER), "--output", str(TRACE_ROOT)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert (
        "refusing to write generated output inside canonical fixture" in result.stderr
    )
