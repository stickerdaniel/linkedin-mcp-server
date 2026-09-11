"""Canonical semantic trace checks for extractor policy."""

from __future__ import annotations

from functools import wraps
from pathlib import Path
from typing import Any
from unittest.mock import patch

import importlib.util
import json
import logging
import subprocess
import sys

import pytest

from linkedin_mcp_server.core.exceptions import AuthenticationError
from linkedin_mcp_server.scraping.extractor import LinkedInExtractor
from linkedin_mcp_server.scraping.fields import COMPANY_SECTIONS, PERSON_SECTIONS
from linkedin_mcp_server.scraping.person import PersonScraper

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
_CHECKER_SPEC = importlib.util.spec_from_file_location(
    "check_scraping_policy_traces", CHECKER
)
assert _CHECKER_SPEC is not None and _CHECKER_SPEC.loader is not None
_CHECKER_MODULE = importlib.util.module_from_spec(_CHECKER_SPEC)
_CHECKER_SPEC.loader.exec_module(_CHECKER_MODULE)


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


async def test_baseline_provenance_is_generated_from_the_final_production_parent():
    provenance = (await build_policy_traces())["baseline-provenance.json"]["result"]

    assert provenance["production_baseline"] == policy_scenarios.PRODUCTION_BASELINE
    assert provenance["python_version"] == "3.13"
    assert len(provenance["uv_lock_sha256"]) == 64
    assert provenance["resolved_dependencies"]["fastmcp"] == ["3.4.4"]
    assert provenance["resolved_dependencies"]["patchright"] == ["1.61.2"]
    assert provenance["extractor_inventory"] == {
        "line_count": 6436,
        "method_count": 86,
        "public_coroutine_count": 20,
        "public_coroutines": sorted(
            TOOL_FACADE_METHODS | {"get_page_text", "click_button_by_text"}
        ),
        "mutable_instance_attributes": ["_page", "_scroll_seconds"],
    }


def test_check_mode_uses_guarded_provenance_when_baseline_object_missing(monkeypatch):
    def reject_baseline_read(_path: str) -> bytes:
        raise AssertionError("missing baseline must not inspect relocated production")

    scenario_globals = _CHECKER_MODULE.build_policy_traces.__globals__
    monkeypatch.setitem(scenario_globals, "_baseline_object_exists", lambda: False)
    monkeypatch.setitem(scenario_globals, "_baseline_file", reject_baseline_read)
    monkeypatch.setattr(sys, "argv", [str(CHECKER), "--check"])

    assert _CHECKER_MODULE.main() == 0


def test_missing_baseline_rejects_tampered_canonical_provenance(monkeypatch, tmp_path):
    trace_root = tmp_path / "v1"
    trace_root.mkdir()
    raw = (TRACE_ROOT / "baseline-provenance.json").read_bytes()
    tampered = raw.replace(b'"python_version": "3.13"', b'"python_version": "3.12"')
    assert tampered != raw
    (trace_root / "baseline-provenance.json").write_bytes(tampered)
    monkeypatch.setattr(policy_scenarios, "_baseline_object_exists", lambda: False)
    monkeypatch.setattr(policy_scenarios, "TRACE_ROOT", trace_root)

    with pytest.raises(AssertionError, match="baseline provenance hash mismatch"):
        policy_scenarios._baseline_provenance_trace()


async def test_trace_set_exercises_every_tool_facing_facade_method():
    traces = await build_policy_traces()
    called = {
        trace["call"]["method"]
        for trace in traces.values()
        if trace["call"]["method"] in TOOL_FACADE_METHODS
    }

    assert called == TOOL_FACADE_METHODS


async def test_tool_schema_trace_keeps_people_boundary_coercion_and_inventory():
    schemas = (await build_policy_traces())["facade-contract.json"]["result"][
        "tool_schemas"
    ]

    assert len(schemas) == 19
    network = schemas["search_people"]["input"]["properties"]["network"]
    assert network["anyOf"] == [
        {"items": {"type": "string"}, "type": "array"},
        {"type": "null"},
    ]
    assert 'comma-separated string ("F,S") is also' in network["description"]
    assert schemas["send_message"]["input"]["required"] == [
        "linkedin_username",
        "message",
        "confirm_send",
    ]


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
    original = PersonScraper.scrape_person
    removed: list[dict[str, Any]] = []

    @wraps(original)
    async def drop_references(self: Any, *args: Any, **kwargs: Any) -> dict[str, Any]:
        result = await original(self, *args, **kwargs)
        references = result.pop("references", None)
        if references:
            removed.append(references)
        return result

    with patch.object(PersonScraper, "scrape_person", drop_references):
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
    dry_run = traces["message-dry-run.json"]

    assert len(saved["result"]["job_ids"]) == 20
    assert saved["result"]["reference_count"] == 15
    assert saved["result"]["references"]["saved_jobs"][0]["text"] == (
        "Senior policy engineer with richer duplicate metadata"
    )
    assert dry_run["result"]["status"] == "confirmation_required"
    assert dry_run["result"]["recipient_selected"] is True
    assert not any(
        event["kind"] in {"evaluate_handle", "handle.evaluate"}
        for event in dry_run["events"]
    )


async def test_message_target_resolution_distinguishes_handoff_from_failure():
    traces = await build_policy_traces()
    unavailable = traces["message-target-unavailable.json"]
    unresolved = traces["message-target-unresolved.json"]

    assert unavailable["result"]["status"] == "message_unavailable"
    assert "connect_with_person" in unavailable["result"]["message"]
    assert unresolved["result"]["status"] == "recipient_resolution_failed"
    assert "connect_with_person" not in unresolved["result"]["message"]
    assert unavailable["result"]["retry_safe"] is True
    assert unresolved["result"]["retry_safe"] is True
    assert all(
        not any(event["kind"] == "evaluate_handle" for event in trace["events"])
        for trace in (unavailable, unresolved)
    )


async def test_message_occupied_drafts_are_never_submitted_or_mutated():
    traces = await build_policy_traces()
    existing = traces["message-composer-occupied.json"]
    restored = traces["message-composer-restored.json"]

    assert existing["result"]["status"] == "composer_occupied"
    assert restored["result"]["status"] == "composer_occupied"
    assert all(trace["result"]["retry_safe"] is True for trace in (existing, restored))
    assert not any(event["kind"] == "evaluate_handle" for event in existing["events"])
    restored_operations = _operation_positions(restored)
    assert restored_operations["message_composer_write"]
    assert restored_operations["message_composer_cleanup"]
    assert "message_submit" not in restored_operations
    assert "message_confirmation_prepare" not in restored_operations


async def test_message_pins_owner_route_and_submit_before_same_node_evidence():
    sent = (await build_policy_traces())["message-sent.json"]
    positions = _operation_positions(sent)

    assert sent["result"]["status"] == "sent"
    assert sent["result"]["sent"] is True
    assert sent["result"]["retry_safe"] is False
    assert (
        positions["message_composer_owner"][0]
        < positions["message_composer_write"][0]
        < positions["message_submit_ready"][0]
        < positions["message_confirmation_prepare"][0]
        < positions["message_submit"][0]
        < positions["message_confirmation_ready"][0]
        < positions["message_confirmation_dispose"][0]
        < positions["message_composer_dispose"][0]
    )
    readiness = [
        event
        for event in sent["events"]
        if event.get("operation") == "message_submit_ready"
    ]
    assert len(readiness) == 2
    owner = next(
        event for event in sent["events"] if event["kind"] == "evaluate_handle"
    )
    assert owner["arg"] == {
        "expectedRoute": policy_scenarios._MESSAGE_ROUTE,
        "target": policy_scenarios._MESSAGE_TARGET,
    }
    confirmation = next(
        event
        for event in sent["events"]
        if event.get("operation") == "message_confirmation_ready"
    )
    assert confirmation["arg"] == {
        **policy_scenarios._MESSAGE_TARGET,
        "expected": "New text",
        "owner": {"handle": "handle-1"},
        "token": "confirmation-1",
    }


async def test_message_retry_safety_and_cleanup_follow_dispatch_boundary():
    traces = await build_policy_traces()
    pre_submit = traces["message-pre-submit-cleanup.json"]
    rejected = traces["message-submit-rejected.json"]
    interrupted = traces["message-submit-interrupted.json"]
    unconfirmed = traces["message-unconfirmed.json"]

    for trace in (pre_submit, rejected):
        assert trace["result"]["retry_safe"] is True
        operations = _operation_positions(trace)
        assert operations["message_composer_cleanup"]
        assert operations["message_composer_dispose"]
    assert "message_confirmation_prepare" not in _operation_positions(pre_submit)
    assert rejected["result"]["status"] == "send_unavailable"

    for trace in (interrupted, unconfirmed):
        assert trace["result"]["status"] == "send_unconfirmed"
        assert trace["result"]["sent"] is False
        assert trace["result"]["retry_safe"] is False
        operations = _operation_positions(trace)
        assert operations["message_confirmation_dispose"]
        assert operations["message_composer_dispose"]
        assert "message_composer_cleanup" not in operations


async def test_message_cancellation_propagates_after_owner_cleanup():
    trace = (await build_policy_traces())["message-cancelled.json"]
    positions = _operation_positions(trace)

    assert trace["result"] == {"raised": "CancelledError"}
    assert positions["message_submit"][0] < positions["message_confirmation_ready"][0]
    assert (
        positions["message_confirmation_ready"][0]
        < positions["message_confirmation_dispose"][0]
        < positions["message_composer_dispose"][0]
        < positions["handle.dispose"][0]
    )


async def test_invalid_message_guards_cover_blank_c0_and_del_before_browser():
    traces = await build_policy_traces()
    for name in ("message-blank.json", "message-c0.json", "message-del.json"):
        trace = traces[name]
        assert trace["result"]["status"] == "invalid_message"
        assert trace["result"]["sent"] is False
        assert trace["result"]["retry_safe"] is True
        assert trace["events"] == []


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
