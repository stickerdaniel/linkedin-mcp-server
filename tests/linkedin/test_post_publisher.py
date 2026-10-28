"""PostPublisher against a fake composer: what it types, clicks and cleans up."""

from __future__ import annotations

from typing import Any, cast

import pytest

from linkedin_mcp_server.linkedin import post_publisher
from linkedin_mcp_server.linkedin.navigation import PageNavigator
from linkedin_mcp_server.linkedin.post_publisher import (
    COMPOSER_URL,
    MAX_POST_CHARS,
    PostPublisher,
)
from linkedin_mcp_server.linkedin.session import PageSession


class FakeComposer:
    """The share composer as the page script sees it, with a keyboard."""

    def __init__(
        self,
        *,
        opens: bool = True,
        initial_text: str = "",
        post_buttons: int = 1,
        post_enabled: bool = True,
        mangle: bool = False,
        closes_on_post: bool = True,
        post_raises: bool = False,
        reads_raise_after_post: bool = False,
        post_reply_empty: bool = False,
        dismiss_fails: bool = False,
        typing_raises: bool = False,
    ) -> None:
        self.dialog_open = opens
        self.text = initial_text
        self.post_buttons = post_buttons
        self.post_enabled = post_enabled
        self.mangle = mangle
        self.closes_on_post = closes_on_post
        self.post_raises = post_raises
        self.reads_raise_after_post = reads_raise_after_post
        self.post_reply_empty = post_reply_empty
        self.dismiss_fails = dismiss_fails
        self.typing_raises = typing_raises
        self.selected = False
        self.posted = False
        self.keys: list[str] = []
        self.keyboard = self

    # keyboard
    async def insert_text(self, text: str) -> None:
        if self.typing_raises:
            raise RuntimeError("keyboard failed")
        self.text += text + ("!" if self.mangle else "")

    async def press(self, key: str) -> None:
        self.keys.append(key)
        if key == "Enter":
            # The editor renders a paragraph break as several newlines.
            self.text += "\n\n\n"
        elif key == "ControlOrMeta+A":
            self.selected = True
        elif key == "Backspace" and self.selected:
            self.text, self.selected = "", False

    # page script
    def script(self, mode: str) -> dict[str, Any]:
        if self.posted and mode == "read" and self.reads_raise_after_post:
            raise RuntimeError("page closed while reading")
        if not self.dialog_open:
            return {"dialogs": 0, "editors": 0, "text": "", "postButtons": 0}
        state: dict[str, Any] = {
            "dialogs": 1,
            "editors": 1,
            "text": self.text,
            "postButtons": self.post_buttons,
            "postEnabled": self.post_buttons == 1 and self.post_enabled,
            "result": None,
        }
        if mode == "post":
            if state["postEnabled"]:
                self.posted = True
                self.dialog_open = not self.closes_on_post
                if self.post_raises:
                    raise RuntimeError("page closed after the click")
                if self.post_reply_empty:
                    return {}
                state["result"] = "clicked"
            else:
                state["result"] = "unavailable"
        elif mode == "dismiss":
            self.dialog_open = self.dismiss_fails
            state["result"] = "clicked"
        elif mode == "focus":
            state["result"] = "focused"
        return state


class FakeSession:
    def __init__(self, composer: FakeComposer) -> None:
        self.page = composer
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    async def delay(self, seconds: float) -> None:
        self.now += seconds

    async def check_rate_limit(self) -> None:
        return None

    async def run_on_linkedin(self, script: str, arg: Any = None) -> dict[str, Any]:
        assert script is post_publisher._COMPOSER_JS
        return self.page.script(arg)


class FakeNavigator:
    def __init__(self) -> None:
        self.urls: list[str] = []

    async def _navigate_to_page(self, url: str, *, deadline: float | None = None):
        self.urls.append(url)


def make(**kwargs: Any) -> tuple[PostPublisher, FakeComposer, FakeNavigator]:
    composer = FakeComposer(**kwargs)
    navigator = FakeNavigator()
    publisher = PostPublisher(
        cast(PageSession, FakeSession(composer)), cast(PageNavigator, navigator)
    )
    return publisher, composer, navigator


async def test_a_dry_run_types_verifies_and_leaves_nothing_behind():
    publisher, composer, navigator = make()

    result = await publisher.create_post("Hello\n\nWorld", confirm_post=False)

    assert result["status"] == "dry_run"
    assert result["text_matches"] is True
    assert result["published"] is False
    assert result["retry_safe"] is True
    assert composer.posted is False
    # Emptied before it was dismissed, so LinkedIn never asks about a draft.
    assert composer.text == "" and composer.dialog_open is False
    assert navigator.urls == [COMPOSER_URL]


async def test_a_confirmed_post_publishes_and_never_retries_blindly():
    publisher, composer, _ = make()

    result = await publisher.create_post("Hello", confirm_post=True)

    assert result["status"] == "published"
    assert result["published"] is True
    assert result["retry_safe"] is False
    assert composer.posted is True
    # Nothing touches the composer once Post was clicked.
    assert "ControlOrMeta+A" not in composer.keys


async def test_a_post_whose_composer_stays_open_is_unconfirmed_and_not_retryable():
    publisher, composer, _ = make(closes_on_post=False)

    result = await publisher.create_post("Hello", confirm_post=True)

    assert result["status"] == "unconfirmed"
    assert result["published"] is False
    assert result["retry_safe"] is False
    assert composer.dialog_open is True


@pytest.mark.parametrize(
    "kwargs",
    [
        {"post_raises": True},
        {"reads_raise_after_post": True},
        {"post_reply_empty": True},
    ],
    ids=["click-reply-raises", "closure-check-raises", "click-reply-empty"],
)
async def test_a_click_whose_outcome_is_unknown_is_unconfirmed_and_left_alone(kwargs):
    publisher, composer, _ = make(**kwargs)

    result = await publisher.create_post("Hello", confirm_post=True)

    assert result["status"] == "unconfirmed"
    assert result["published"] is False
    assert result["retry_safe"] is False
    # The composer may already be submitting, so it is never cleared or closed.
    assert composer.text == "Hello"
    assert "ControlOrMeta+A" not in composer.keys


async def test_an_error_before_the_click_still_cleans_up_and_propagates():
    publisher, composer, _ = make(typing_raises=True)

    with pytest.raises(RuntimeError, match="keyboard failed"):
        await publisher.create_post("Hello", confirm_post=True)

    assert composer.posted is False
    assert composer.dialog_open is False


async def test_a_dry_run_that_cannot_close_the_composer_says_so():
    publisher, composer, _ = make(dismiss_fails=True)

    result = await publisher.create_post("Hello", confirm_post=False)

    assert result["status"] == "dry_run"
    assert result["composer_closed"] is False
    assert "leftover text" in result["message"]
    assert composer.dialog_open is True


async def test_a_clean_dry_run_has_no_cleanup_warning():
    publisher, _, _ = make()

    result = await publisher.create_post("Hello", confirm_post=False)

    assert "composer_closed" not in result


async def test_paragraph_padding_added_by_the_editor_is_not_a_mismatch():
    publisher, _, _ = make()

    result = await publisher.create_post("One\n\nTwo\nThree", confirm_post=False)

    assert result["status"] == "dry_run"


async def test_text_that_differs_in_the_composer_is_never_posted():
    publisher, composer, _ = make(mangle=True)

    result = await publisher.create_post("Hello", confirm_post=True)

    assert result["status"] == "text_mismatch"
    assert composer.posted is False
    assert composer.dialog_open is False


@pytest.mark.parametrize(
    "kwargs",
    [{"post_buttons": 2}, {"post_buttons": 0}, {"post_enabled": False}],
    ids=["ambiguous", "missing", "disabled"],
)
async def test_the_post_button_must_be_one_enabled_match(kwargs):
    publisher, composer, _ = make(**kwargs)

    result = await publisher.create_post("Hello", confirm_post=True)

    assert result["status"] == "post_button_unavailable"
    assert composer.posted is False
    assert composer.dialog_open is False


async def test_a_composer_already_holding_a_draft_is_left_alone():
    publisher, composer, _ = make(initial_text="Half-written draft")

    result = await publisher.create_post("Hello", confirm_post=True)

    assert result["status"] == "composer_occupied"
    assert composer.text == "Half-written draft"
    assert composer.dialog_open is True
    assert composer.keys == []


async def test_a_composer_that_never_opens_is_reported():
    publisher, composer, _ = make(opens=False)

    result = await publisher.create_post("Hello", confirm_post=True)

    assert result["status"] == "composer_unavailable"
    assert composer.posted is False


@pytest.mark.parametrize(
    "text",
    ["", " \n\t ", "x" * (MAX_POST_CHARS + 1), "bell\x07", "nul\x00", "del\x7f"],
    ids=["empty", "blank", "too-long", "bell", "nul", "del"],
)
async def test_text_that_cannot_be_posted_is_refused_before_navigation(text):
    publisher, composer, navigator = make()

    result = await publisher.create_post(text, confirm_post=True)

    assert result["status"] == "invalid"
    assert result["retry_safe"] is True
    assert navigator.urls == []
    assert composer.posted is False


def test_the_longest_allowed_post_is_accepted():
    assert post_publisher.refuse_an_invalid_post("x" * MAX_POST_CHARS) is None
