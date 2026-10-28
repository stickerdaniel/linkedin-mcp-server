"""A write step that never finishes stops with its name instead of hanging."""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import MagicMock

import pytest

from linkedin_mcp_server.linkedin.profile_editor import ProfileEditor
from linkedin_mcp_server.profile_edit.errors import (
    ProfileEditError,
    ProfileEditErrorCode,
)


def editor() -> ProfileEditor:
    return ProfileEditor(MagicMock(), MagicMock())


async def test_a_hung_step_stops_with_its_name():
    never = asyncio.Event()
    with pytest.raises(ProfileEditError) as e:
        await editor()._phase("type skill", never.wait(), limit=0.05)
    assert e.value.code is ProfileEditErrorCode.LINKEDIN_SAVE_FAILED
    assert e.value.details["step"] == "type skill"
    assert "Nothing was saved" in e.value.message


async def test_a_hung_save_says_the_outcome_is_unknown():
    with pytest.raises(ProfileEditError) as e:
        await editor()._phase("save", asyncio.Event().wait(), limit=0.05)
    assert "may or may not have gone through" in e.value.message


async def test_a_slow_step_is_logged_and_its_result_returned(monkeypatch, caplog):
    import linkedin_mcp_server.linkedin.profile_editor as module

    monkeypatch.setattr(module, "_SLOW_PHASE_S", 0.01)

    async def slow() -> str:
        await asyncio.sleep(0.03)
        return "done"

    with caplog.at_level(logging.WARNING, logger=module.__name__):
        assert await editor()._phase("pick suggestion", slow(), limit=5) == "done"
    assert "pick suggestion" in caplog.text
