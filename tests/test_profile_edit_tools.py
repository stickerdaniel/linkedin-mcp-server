"""The profile-edit tools as an MCP client sees them."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastmcp import FastMCP

from linkedin_mcp_server.tools.profile_edit import register_profile_edit_tools
from profile_edit_fakes import FakeEditor

READY = "linkedin_mcp_server.tools.profile_edit.get_ready_profile_editor"


@pytest.fixture
def mcp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FastMCP:
    monkeypatch.setenv("LINKEDIN_PROFILE_EDITS_DIR", str(tmp_path))
    monkeypatch.delenv("MCP_LINKEDIN_WRITE_ENABLED", raising=False)
    server = FastMCP("test")
    register_profile_edit_tools(server)
    return server


async def call(mcp: FastMCP, name: str, args: dict) -> dict:
    result = await mcp.call_tool(name, args)
    assert result.structured_content is not None
    return result.structured_content


async def test_only_apply_is_marked_as_modifying_external_state(mcp):
    tools = {t.name: t for t in await mcp.list_tools()}
    assert set(tools) == {
        "get_my_editable_profile",
        "get_my_experience",
        "get_my_skills",
        "propose_profile_changes",
        "preview_profile_changes",
        "apply_profile_changes",
        "discard_profile_changes",
    }
    assert tools["apply_profile_changes"].annotations.destructive_hint is True
    assert "This modifies external state" in (
        tools["apply_profile_changes"].description or ""
    )
    for name in (
        "get_my_editable_profile",
        "get_my_experience",
        "get_my_skills",
        "propose_profile_changes",
        "preview_profile_changes",
    ):
        assert tools[name].annotations.read_only_hint is True, name
        assert not tools[name].annotations.destructive_hint, name
    assert "Does not modify LinkedIn" in (
        tools["propose_profile_changes"].description or ""
    )


async def test_the_full_workflow_with_both_safeguards(mcp, monkeypatch):
    ed = FakeEditor()
    with patch(READY, AsyncMock(return_value=ed)):
        cs = await call(
            mcp,
            "propose_profile_changes",
            {"changes": {"headline": "Senior Product Engineer"}},
        )
        cs_id = cs["changeSetId"]
        assert cs["status"] == "PENDING_APPROVAL" and ed.writes == []

        no_confirm = await call(mcp, "apply_profile_changes", {"changeSetId": cs_id})
        assert no_confirm["error"] == "CONFIRMATION_REQUIRED"
        disabled = await call(
            mcp, "apply_profile_changes", {"changeSetId": cs_id, "confirm": True}
        )
        assert disabled["error"] == "WRITES_DISABLED" and ed.writes == []

        monkeypatch.setenv("MCP_LINKEDIN_WRITE_ENABLED", "true")
        applied = await call(
            mcp, "apply_profile_changes", {"changeSetId": cs_id, "confirm": True}
        )
        assert (
            applied["status"] == "APPLIED" and applied["results"][0]["verified"] is True
        )
        again = await call(
            mcp, "apply_profile_changes", {"changeSetId": cs_id, "confirm": True}
        )
        assert again["error"] == "CHANGE_SET_NOT_PENDING"
    assert ed.headline == "Senior Product Engineer"


async def test_refusals_never_start_a_browser(mcp, monkeypatch):
    monkeypatch.setenv("MCP_LINKEDIN_WRITE_ENABLED", "true")
    ready = AsyncMock(return_value=FakeEditor())
    with patch(READY, ready):
        cs = await call(
            mcp, "propose_profile_changes", {"changes": {"about": "New about."}}
        )
    ready.reset_mock()
    ready.side_effect = AssertionError("browser acquired")
    with patch(READY, ready):
        assert (
            await call(
                mcp,
                "apply_profile_changes",
                {"changeSetId": cs["changeSetId"], "confirm": False},
            )
        )["error"] == "CONFIRMATION_REQUIRED"
        assert (
            await call(
                mcp, "discard_profile_changes", {"changeSetId": cs["changeSetId"]}
            )
        )["status"] == "DISCARDED"
        assert (
            await call(
                mcp,
                "apply_profile_changes",
                {"changeSetId": cs["changeSetId"], "confirm": True},
            )
        )["error"] == "CHANGE_SET_NOT_PENDING"
    ready.assert_not_awaited()


async def test_unsupported_fields_are_named_not_silently_dropped(mcp):
    ready = AsyncMock(side_effect=AssertionError("browser acquired"))
    with patch(READY, ready):
        out = await call(
            mcp,
            "propose_profile_changes",
            {"changes": {"headline": "X", "location": "London", "education": []}},
        )
    assert out["error"] == "UNSUPPORTED_FIELD" and out["details"]["fields"] == [
        "education",
        "location",
    ]


async def test_a_security_checkpoint_is_reported_not_worked_around(mcp):
    from linkedin_mcp_server.core.exceptions import RateLimitError

    ed = FakeEditor(
        fail={"read_identity": RateLimitError("LinkedIn security checkpoint detected.")}
    )
    with patch(READY, AsyncMock(return_value=ed)):
        out = await call(mcp, "get_my_editable_profile", {})
    assert out["error"] == "AUTHENTICATION_REQUIRED"


async def test_nested_unsupported_fields_are_named_too(mcp):
    ready = AsyncMock(side_effect=AssertionError("browser acquired"))
    with patch(READY, ready):
        out = await call(
            mcp,
            "propose_profile_changes",
            {
                "changes": {
                    "experiences": [
                        {
                            "experienceId": "1",
                            "location": "London",
                            "match": {"endDate": "2020"},
                        }
                    ],
                    "skills": {"add": ["Go"], "reorder": ["Go"]},
                }
            },
        )
    assert out["error"] == "UNSUPPORTED_FIELD"
    assert out["details"]["fields"] == [
        "experiences[0].location",
        "experiences[0].match.endDate",
        "skills.reorder",
    ]
