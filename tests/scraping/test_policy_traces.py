"""Canonical semantic trace checks for extractor policy."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import json
import subprocess
import sys

from linkedin_mcp_server.scraping.fields import COMPANY_SECTIONS, PERSON_SECTIONS

from .policy_scenarios import (
    TOOL_FACADE_METHODS,
    TRACE_ROOT,
    build_policy_traces,
    canonical_json,
    policy_trace_diff,
)


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
    assert saved["result"]["references"][0]["text"] == (
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
