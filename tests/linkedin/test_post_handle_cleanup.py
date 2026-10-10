"""Owned editor handles are released without replacing results or cancellation."""

import asyncio
from unittest.mock import AsyncMock
from typing import cast

from patchright.async_api import ElementHandle

import anyio
import pytest

from .test_post_actions import FakePage, POST_URL, actions


@pytest.mark.parametrize(
    "outcome", ["confirmed", "type_refused", "submit_refused", "cancelled"]
)
async def test_editor_handle_released_on_every_owned_exit(monkeypatch, outcome):
    page = FakePage(units=0)
    page.editor.dispose = AsyncMock()
    owner = actions(page)
    monkeypatch.setattr(
        owner,
        "_type_text",
        AsyncMock(return_value="not_owned" if outcome == "type_refused" else "typed"),
    )
    monkeypatch.setattr(
        owner,
        "_submit_editor",
        AsyncMock(
            return_value="not_owned" if outcome == "submit_refused" else "submitted"
        ),
    )
    expected = {"acted": True, "retry_safe": False}
    monkeypatch.setattr(
        owner,
        "_confirm_text",
        AsyncMock(
            return_value=expected,
            side_effect=asyncio.CancelledError if outcome == "cancelled" else None,
        ),
    )
    call = owner._write_and_submit(
        cast(ElementHandle, page.handle),
        POST_URL,
        "Hello",
        success_status="commented",
        unconfirmed_status="comment_unconfirmed",
        noun="comment",
    )
    if outcome == "cancelled":
        with pytest.raises(asyncio.CancelledError):
            await call
    elif outcome == "confirmed":
        assert await call is expected
    else:
        assert (await call)["retry_safe"] is True
    page.editor.dispose.assert_awaited_once()


@pytest.mark.parametrize("mode", ["stall", "error", "cancel_during_cleanup"])
async def test_editor_cleanup_is_bounded_and_external_cancel_survives(
    monkeypatch, mode
):
    page = FakePage(units=0)
    owner = actions(page)
    monkeypatch.setattr(owner, "_type_text", AsyncMock(return_value="typed"))
    monkeypatch.setattr(owner, "_submit_editor", AsyncMock(return_value="submitted"))
    expected = {"acted": True, "retry_safe": False}
    monkeypatch.setattr(owner, "_confirm_text", AsyncMock(return_value=expected))

    async def dispose():
        if mode == "error":
            raise RuntimeError("browser connection closed")
        if mode == "cancel_during_cleanup":
            outer.cancel()
        await anyio.sleep_forever()

    page.editor.dispose = AsyncMock(side_effect=dispose)
    with anyio.fail_after(0.5) as outer:
        result = await owner._write_and_submit(
            cast(ElementHandle, page.handle),
            POST_URL,
            "Hello",
            success_status="commented",
            unconfirmed_status="comment_unconfirmed",
            noun="comment",
        )
        assert mode != "cancel_during_cleanup", "external cancellation was swallowed"
        assert result is expected
    page.editor.dispose.assert_awaited_once()
