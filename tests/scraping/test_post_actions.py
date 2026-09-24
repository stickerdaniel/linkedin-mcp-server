"""Tests for the post-engagement owner.

What these hold is the set of refusals, because every one of them guards a
public write. A reaction that is already pressed is not clicked, an
unconfirmed comment is not reported as published, and a flyout or menu whose
item count is not the one this code knows how to index refuses instead of
landing one position off.

``page.evaluate`` is a mock here, so none of the extractor programs run and
their answers are supplied directly; ``tests/test_post_actions_dom.py`` covers
those against a real DOM in four label sets. The two halves are complementary
and neither substitutes for the other: this file proves the flow control, that
one proves the queries.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from patchright.async_api import Page

import linkedin_mcp_server.scraping.post_actions as post_actions
from linkedin_mcp_server.scraping.contracts import POST_ACTION_INTERRUPTED_WARNING
from linkedin_mcp_server.core.exceptions import InvalidReferenceError
from linkedin_mcp_server.scraping.navigation import PageNavigator
from linkedin_mcp_server.scraping.post_actions import PostActions
from linkedin_mcp_server.scraping.session import ScrapingSession

PERMALINK = "/feed/update/urn:li:ugcPost:7506667649444237313/"
POST_URL = "https://www.linkedin.com/feed/update/urn:li:ugcPost:7506667649444237313/"
POST_ID = "7506667649444237313"

# Every extractor program, by the short name the page double takes as a keyword.
# Routing on the constant itself rather than on a text match keeps a renamed
# program from silently falling through to a default answer.
PROGRAMS = {
    "signals": post_actions.POST_ACTION_SIGNALS_JS,
    "react": post_actions.CLICK_REACT_TOGGLE_JS,
    "flyout": post_actions.READ_REACTION_FLYOUT_JS,
    "pick_reaction": post_actions.CLICK_REACTION_JS,
    "open_repost": post_actions.OPEN_REPOST_MENU_JS,
    "repost_menu": post_actions.READ_REPOST_MENU_JS,
    "pick_repost": post_actions.CLICK_REPOST_MENU_ITEM_JS,
    "own": post_actions.OWN_EDITOR_JS,
    "clear": post_actions.CLEAR_EDITOR_JS,
    "submit": post_actions.SUBMIT_EDITOR_JS,
    "units": post_actions.COUNT_TEXT_UNITS_JS,
}


def signals(
    *,
    root: bool = True,
    bar: bool = True,
    pressed: bool = False,
    pressed_present: bool = True,
    disabled: bool = False,
    repost_opener: bool = True,
    editors: int = 1,
    counts: list[str] | None = None,
    main: bool = True,
) -> dict[str, Any]:
    """One structural read of a post, in the shape the program returns."""
    return {
        "hasMain": main,
        "hasRoot": root,
        "hasBar": bar,
        "barButtonCount": 4 if bar else 0,
        "reactPressedPresent": pressed_present if bar else False,
        "reactPressed": pressed if bar and pressed_present else None,
        "reactDisabled": disabled if bar else None,
        "hasRepostOpener": repost_opener,
        "editorCount": editors,
        "barText": "",
        "counts": counts if counts is not None else ["12", "3", "1"],
    }


class FakeHandle:
    """A handle double that records disposal and hands out elements.

    ``as_element`` answering ``None`` is how the real API reports a program
    that returned null, which is what an unpinnable post looks like.
    """

    def __init__(self, *, pinned: bool = True):
        self._pinned = pinned
        self.disposed = False
        self.element = MagicMock()
        self.element.hover = AsyncMock()
        self.element.dispose = AsyncMock()
        self.element.evaluate_handle = AsyncMock(side_effect=self._toggle)

    def as_element(self) -> Any:
        return self if self._pinned else None

    async def _toggle(self, script: str) -> Any:
        assert "__linkedinMcpPost" in script
        holder = MagicMock()
        holder.as_element = MagicMock(return_value=self.element)
        return holder

    async def evaluate_handle(self, script: str) -> Any:
        return await self._toggle(script)

    async def dispose(self) -> None:
        self.disposed = True


class FakeEditor:
    """An editor that holds what the keyboard actually typed into it.

    Scripting the read-back instead would make every text check in the flow
    agree with itself by construction. Here the keystrokes accumulate and the
    read-back reports them, so ``drops`` can model the thing this guard exists
    for: an autocomplete popup swallowing part of what was typed.
    """

    def __init__(self, *, focusable: bool = True, drops: str = ""):
        self.focusable = focusable
        self.drops = drops
        self.text = ""
        self.clicked = False

    async def click(self) -> None:
        self.clicked = True

    async def evaluate(self, script: str) -> Any:
        if "activeElement" in script:
            # Focus follows the real click, never a bare selector match.
            return self.clicked and self.focusable
        if "innerText" in script:
            return self.text
        raise AssertionError(f"unexpected editor program: {script}")

    def type(self, chunk: str) -> None:
        self.text += chunk.replace(self.drops, "") if self.drops else chunk


class FakeProperty:
    """One property of a returned JS object, read as a value or an element."""

    def __init__(self, *, value: Any = None, element: Any = None):
        self._value = value
        self._element = element

    async def json_value(self) -> Any:
        return self._value

    def as_element(self) -> Any:
        return self._element


class FakePinnedEditor:
    """The ``{status, editor}`` handle the editor pin program returns."""

    def __init__(self, status: str, editor: FakeEditor | None):
        self._status = status
        self._editor = editor
        self.disposed = False

    async def dispose(self) -> None:
        self.disposed = True

    async def get_property(self, name: str) -> FakeProperty:
        if name == "status":
            return FakeProperty(value=self._status)
        if name == "editor":
            return FakeProperty(element=self._editor)
        raise AssertionError(f"unexpected property: {name}")


class FakePage:
    """A page double that answers each extractor program from a script.

    Every keyword takes either one answer for all calls or a list consumed in
    order, so a flow that re-reads state can be given a transition. ``calls``
    records which programs ran, which is how a test asserts that nothing was
    clicked.
    """

    def __init__(
        self,
        *,
        pinned: bool = True,
        editor: FakeEditor | None = None,
        editor_status: str = "pinned",
        dialog_pinned: bool = True,
        **answers: Any,
    ):
        self.url = POST_URL
        self.calls: list[str] = []
        self._answers = {name: answers.get(name) for name in PROGRAMS}
        self._sequences = {
            name: list(value) if isinstance(value, list) else None
            for name, value in self._answers.items()
        }
        self._last: dict[str, Any] = {}
        self.handle = FakeHandle(pinned=pinned)
        self.editor = editor if editor is not None else FakeEditor()
        self._editor_status = editor_status
        self._dialog_pinned = dialog_pinned
        self.pin_editor_scope: Any = None
        self.keyboard = MagicMock()
        self.keyboard.press = AsyncMock(side_effect=self._press)
        self.keyboard.type = AsyncMock(side_effect=self._type)
        self.wait_for_selector = AsyncMock()
        self.dialog_wait = AsyncMock()

    async def _type(self, text: str, delay: int | None = None) -> None:
        self.calls.append("type")
        self.editor.type(text)

    async def _press(self, key: str) -> None:
        self.calls.append(f"press:{key}")
        if key == "Shift+Enter":
            self.editor.type("\n")

    async def evaluate(self, script: str, *args: Any) -> Any:
        for name, program in PROGRAMS.items():
            if script is program:
                self.calls.append(name)
                if name == "pick_reaction":
                    self.picked_reaction = args[0]
                if name == "pick_repost":
                    self.picked_repost = args[0]
                sequence = self._sequences[name]
                if sequence is None:
                    return self._answers[name]
                # A consumed sequence keeps answering with its last value, so
                # a confirmation loop that polls more than the scripted number
                # of times reads a settled page rather than running dry.
                if sequence:
                    self._last[name] = sequence.pop(0)
                return self._last[name]
        # Anything else is the rate-limit probe's body read, which only runs
        # on a page without <main>; the locator below says this one has it.
        return ""

    async def evaluate_handle(self, script: str, *, arg: Any = None) -> Any:
        # Keyword-only, matching the strict double in
        # `tests/scraping/support/policy_trace.py`: a positional argument there
        # is an undeclared call, so production has to pass this one by name.
        if script is post_actions.PIN_VISIBLE_DIALOG_JS:
            self.calls.append("pin_dialog")
            self.dialog_handle = FakeHandle(pinned=self._dialog_pinned)
            return self.dialog_handle
        if script is post_actions.PIN_EDITOR_JS:
            self.calls.append("pin_editor")
            self.pin_editor_scope = None if arg is None else arg.get("scope")
            status = self._editor_status
            self.pinned_editor = FakePinnedEditor(
                status, self.editor if status == "pinned" else None
            )
            return self.pinned_editor
        assert script is post_actions.PIN_POST_ROOT_JS
        assert arg == POST_ID
        self.calls.append("pin")
        return self.handle

    def locator(self, selector: str) -> Any:
        locator = MagicMock()
        # `main` is present, which is what stops `detect_rate_limit` from
        # treating this as an error-shaped page.
        locator.count = AsyncMock(return_value=1)
        locator.first = locator
        locator.wait_for = self.dialog_wait
        locator.hover = AsyncMock()
        return locator


@pytest.fixture(autouse=True)
def fast_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shrink the real-time waits so a refusal path is not a 12s test.

    The loop counts stay above one so a flow that needs a second read still
    gets one; only the wall-clock cost changes.
    """
    monkeypatch.setattr(post_actions, "_CONFIRM_TIMEOUT", 30)
    monkeypatch.setattr(post_actions, "_FLYOUT_TIMEOUT", 30)
    monkeypatch.setattr(post_actions, "_EDITOR_TIMEOUT", 30)
    monkeypatch.setattr(post_actions, "_CONFIRM_POLL", 0.001)


def actions(page: FakePage) -> PostActions:
    """Wire the owner the way the facade does."""
    session = ScrapingSession(cast(Page, page))
    return PostActions(session, PageNavigator(session))


def navigated() -> Any:
    """Patch the navigation the facade would perform, and record it."""
    return patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock)


class TestReferenceRefusal:
    """Nothing reaches a browser until the reference can name a post."""

    @pytest.mark.parametrize(
        "reference",
        [
            "/in/williamhgates",
            "urn:li:comment:7506667649444237313",
            "/posts/not-a-slug",
            "https://evil.example/posts/a-ugcPost-7506667649444237313-b",
            "",
        ],
    )
    async def test_an_unusable_reference_never_navigates(self, reference: str) -> None:
        page = FakePage()
        with navigated() as navigate:
            with pytest.raises(InvalidReferenceError):
                await actions(page).react_to_post(reference)
        navigate.assert_not_called()
        assert page.calls == []

    async def test_an_unknown_reaction_never_navigates(self) -> None:
        page = FakePage()
        with navigated() as navigate:
            result = await actions(page).react_to_post(PERMALINK, reaction="thumbsup")
        assert result["status"] == "invalid_reaction"
        assert result["acted"] is False
        navigate.assert_not_called()
        assert page.calls == []


class TestPostIdentification:
    """A permalink that does not resolve to exactly one post is refused."""

    async def test_no_matching_post_refuses_without_pinning(self) -> None:
        page = FakePage(signals=signals(root=False))
        with navigated():
            result = await actions(page).react_to_post(PERMALINK)
        assert result["status"] == "post_not_found"
        assert result["acted"] is False
        assert "pin" not in page.calls
        assert "react" not in page.calls

    async def test_a_post_without_an_action_bar_refuses(self) -> None:
        page = FakePage(signals=signals(bar=False))
        with navigated():
            result = await actions(page).comment_on_post(
                PERMALINK, "hello", confirm_comment=True
            )
        assert result["status"] == "actions_unavailable"
        assert page.calls == ["signals"]

    async def test_a_page_without_main_refuses(self) -> None:
        page = FakePage(signals=signals(main=False, root=False))
        with navigated():
            result = await actions(page).react_to_post(PERMALINK)
        assert result["status"] == "post_unavailable"

    async def test_a_pin_that_comes_back_null_refuses_before_clicking(self) -> None:
        page = FakePage(signals=signals(), pinned=False)
        with navigated():
            result = await actions(page).react_to_post(PERMALINK)
        assert result["status"] == "post_not_found"
        assert "react" not in page.calls
        assert page.handle.disposed


class TestReact:
    """The reaction flow, and the one click it must never make."""

    async def test_an_already_pressed_reaction_is_not_clicked_again(self) -> None:
        page = FakePage(signals=signals(pressed=True))
        with navigated():
            result = await actions(page).react_to_post(PERMALINK)
        assert result["status"] == "already_reacted"
        assert result["acted"] is False
        assert result["retry_safe"] is True
        # The whole point: clicking a pressed control retracts the reaction.
        assert "react" not in page.calls
        assert "pin" not in page.calls

    async def test_a_disabled_reaction_control_refuses(self) -> None:
        page = FakePage(signals=signals(disabled=True))
        with navigated():
            result = await actions(page).react_to_post(PERMALINK)
        assert result["status"] == "actions_unavailable"
        assert "react" not in page.calls

    async def test_the_default_reaction_clicks_the_toggle_and_confirms(self) -> None:
        page = FakePage(
            signals=[signals(pressed=False), signals(pressed=True)],
            react="clicked",
        )
        with navigated():
            result = await actions(page).react_to_post(PERMALINK, reaction="like")
        assert result["status"] == "reacted"
        assert result["acted"] is True
        assert result["retry_safe"] is False
        assert result["reaction"] == "like"
        assert result["url"] == POST_URL
        assert "flyout" not in page.calls

    async def test_a_click_that_never_reports_pressed_is_unconfirmed(self) -> None:
        page = FakePage(signals=signals(pressed=False), react="clicked")
        with navigated():
            result = await actions(page).react_to_post(PERMALINK)
        assert result["status"] == "react_unconfirmed"
        assert result["acted"] is False
        # A retry could remove a reaction that did land.
        assert result["retry_safe"] is False

    async def test_a_toggle_without_pressed_state_is_not_clicked(self) -> None:
        page = FakePage(signals=signals(pressed_present=False))
        with navigated():
            result = await actions(page).react_to_post(PERMALINK)
        assert result["status"] == "actions_unavailable"
        assert result["acted"] is False
        assert result["retry_safe"] is True
        assert "react" not in page.calls
        assert "pin" not in page.calls

    async def test_a_cancelled_confirm_still_warns(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        page = FakePage(signals=signals(pressed=False), react="clicked")

        async def boom(_delay: float) -> None:
            raise asyncio.CancelledError()

        with (
            navigated(),
            patch.object(post_actions.asyncio, "sleep", side_effect=boom),
            caplog.at_level(
                logging.WARNING, logger="linkedin_mcp_server.scraping.post_actions"
            ),
            pytest.raises(asyncio.CancelledError),
        ):
            await actions(page).react_to_post(PERMALINK)
        assert POST_ACTION_INTERRUPTED_WARNING in caplog.messages

    async def test_a_specific_reaction_is_picked_by_index(self) -> None:
        page = FakePage(
            signals=[signals(pressed=False), signals(pressed=True)],
            flyout={"count": 6},
            pick_reaction=True,
        )
        with navigated():
            result = await actions(page).react_to_post(PERMALINK, reaction="funny")
        assert result["status"] == "reacted"
        assert result["reaction"] == "funny"
        # "funny" is the sixth control, and the index is what the flow sends.
        assert page.picked_reaction == {
            "expected": 6,
            "index": 5,
            "root": page.handle,
        }
        assert "react" not in page.calls

    @pytest.mark.parametrize("offered", [5, 7])
    async def test_a_flyout_of_the_wrong_size_clicks_nothing(
        self, offered: int
    ) -> None:
        page = FakePage(
            signals=signals(pressed=False),
            flyout={"count": offered},
        )
        with navigated():
            result = await actions(page).react_to_post(PERMALINK, reaction="celebrate")
        assert result["status"] == "reaction_picker_changed"
        assert result["acted"] is False
        assert result["retry_safe"] is True
        assert "pick_reaction" not in page.calls

    async def test_a_flyout_that_never_opens_clicks_nothing(self) -> None:
        page = FakePage(signals=signals(pressed=False), flyout={"count": 0})
        with navigated():
            result = await actions(page).react_to_post(PERMALINK, reaction="support")
        assert result["status"] == "reaction_picker_unavailable"
        assert "pick_reaction" not in page.calls
        assert "react" not in page.calls


class TestComment:
    """The comment flow, its confirm gate and its confirmation."""

    async def test_without_confirmation_nothing_is_typed(self) -> None:
        page = FakePage(signals=signals(editors=1))
        with navigated():
            result = await actions(page).comment_on_post(
                PERMALINK, "hello", confirm_comment=False
            )
        assert result["status"] == "confirmation_required"
        assert result["acted"] is False
        assert result["retry_safe"] is True
        assert "type" not in page.calls
        assert "submit" not in page.calls
        assert "pin" not in page.calls

    async def test_a_post_with_no_comment_editor_refuses(self) -> None:
        page = FakePage(signals=signals(editors=0))
        with navigated():
            result = await actions(page).comment_on_post(
                PERMALINK, "hello", confirm_comment=True
            )
        assert result["status"] == "comment_box_unavailable"
        assert "type" not in page.calls

    async def test_two_comment_editors_refuse_rather_than_guess(self) -> None:
        page = FakePage(signals=signals(editors=2))
        with navigated():
            result = await actions(page).comment_on_post(
                PERMALINK, "hello", confirm_comment=True
            )
        assert result["status"] == "comment_box_ambiguous"
        assert "type" not in page.calls

    async def test_a_confirmed_comment_is_typed_submitted_and_verified(self) -> None:
        page = FakePage(
            signals=signals(editors=1),
            units=[0, 1],
            submit="submitted",
        )
        with navigated():
            result = await actions(page).comment_on_post(
                PERMALINK, "Great write-up", confirm_comment=True
            )
        assert result["status"] == "commented"
        assert result["acted"] is True
        assert result["retry_safe"] is False
        assert page.calls.count("submit") == 1
        assert page.handle.disposed

    async def test_the_text_arrives_as_keystrokes_in_a_clicked_editor(self) -> None:
        # Both halves of this are load-bearing against a live comment box and
        # neither is cosmetic. A scripted `focus()` leaves the box inactive, and
        # text written from JavaScript reads back correctly while LinkedIn's own
        # editor state stays empty and its submit control is never drawn. The
        # only path measured to produce a submittable comment is a real click
        # followed by real key events.
        page = FakePage(
            signals=signals(editors=1),
            units=[0, 1],
            submit="submitted",
        )
        with navigated():
            result = await actions(page).comment_on_post(
                PERMALINK, "Great write-up", confirm_comment=True
            )
        assert result["status"] == "commented"
        assert page.editor.clicked
        assert page.editor.text == "Great write-up"
        assert page.calls.index("type") < page.calls.index("submit")
        # The wrapper object handle is released once its properties are read.
        assert page.pinned_editor.disposed

    async def test_a_newline_is_shift_entered_rather_than_entered(self) -> None:
        # A bare Enter in a comment box is a submit on some layouts, which would
        # publish the first line and orphan the rest. The keystroke is the
        # contract here, not the resulting text.
        page = FakePage(
            signals=signals(editors=1),
            units=[0, 1],
            submit="submitted",
        )
        with navigated():
            await actions(page).comment_on_post(
                PERMALINK, "First line\nSecond line", confirm_comment=True
            )
        assert "press:Shift+Enter" in page.calls
        assert "press:Enter" not in page.calls
        assert page.editor.text == "First line\nSecond line"

    async def test_text_that_does_not_arrive_verbatim_is_cleared_unsent(self) -> None:
        # An autocomplete popup eating keystrokes is the live version of this.
        # What must not happen is a submit of whatever did land.
        page = FakePage(
            signals=signals(editors=1),
            units=0,
            editor=FakeEditor(drops="write-up"),
        )
        with navigated():
            result = await actions(page).comment_on_post(
                PERMALINK, "Great write-up", confirm_comment=True
            )
        assert result["status"] == "write_failed"
        assert result["acted"] is False
        assert "submit" not in page.calls
        assert "clear" in page.calls

    async def test_an_editor_that_will_not_focus_is_not_typed_into(self) -> None:
        page = FakePage(
            signals=signals(editors=1),
            units=0,
            editor=FakeEditor(focusable=False),
        )
        with navigated():
            result = await actions(page).comment_on_post(
                PERMALINK, "hello", confirm_comment=True
            )
        assert result["status"] == "write_failed"
        assert "type" not in page.calls
        assert "submit" not in page.calls

    async def test_text_that_never_appears_is_unconfirmed_and_unsafe(self) -> None:
        page = FakePage(
            signals=signals(editors=1),
            units=0,
            submit="submitted",
        )
        with navigated():
            result = await actions(page).comment_on_post(
                PERMALINK, "Great write-up", confirm_comment=True
            )
        assert result["status"] == "comment_unconfirmed"
        assert result["acted"] is False
        assert result["retry_safe"] is False

    async def test_an_identical_earlier_comment_does_not_count_as_this_one(
        self,
    ) -> None:
        # The baseline is read before the submit, so one matching unit that was
        # already on the post is not mistaken for the new one.
        page = FakePage(
            signals=signals(editors=1),
            units=1,
            submit="submitted",
        )
        with navigated():
            result = await actions(page).comment_on_post(
                PERMALINK, "Great write-up", confirm_comment=True
            )
        assert result["status"] == "comment_unconfirmed"

    async def test_the_baseline_is_read_before_anything_is_typed(self) -> None:
        # The editor holding the draft is a descendant of the post, so a count
        # taken after typing includes it and the confirmation compares the
        # draft against itself. Measured live: that comparison passed on a post
        # where nothing was published.
        page = FakePage(
            signals=signals(editors=1),
            units=[0, 1],
            submit="submitted",
        )
        with navigated():
            await actions(page).comment_on_post(
                PERMALINK, "Great write-up", confirm_comment=True
            )
        assert page.calls.index("units") < page.calls.index("type")

    async def test_an_existing_draft_is_left_untouched(self) -> None:
        page = FakePage(
            signals=signals(editors=1),
            units=0,
            editor_status="draft_present",
        )
        with navigated():
            result = await actions(page).comment_on_post(
                PERMALINK, "hello", confirm_comment=True
            )
        assert result["status"] == "draft_present"
        assert result["retry_safe"] is True
        assert "type" not in page.calls
        assert "submit" not in page.calls
        assert "clear" not in page.calls

    async def test_two_submit_candidates_refuse_and_stay_retry_safe(self) -> None:
        page = FakePage(
            signals=signals(editors=1),
            units=0,
            submit="ambiguous_submit",
        )
        with navigated():
            result = await actions(page).comment_on_post(
                PERMALINK, "hello", confirm_comment=True
            )
        assert result["status"] == "submit_unavailable"
        assert result["acted"] is False
        # Nothing was dispatched, so calling again cannot double-publish, and
        # the typed text is taken back so the retry is not met by its own draft.
        assert result["retry_safe"] is True
        assert "clear" in page.calls

    async def test_a_submit_control_that_never_renders_clicks_nothing(self) -> None:
        # The old rule here relaxed to "the only enabled button left" when no
        # `type="submit"` was found. On a live comment box that button was the
        # photo attachment: it was clicked, it opened a file picker, and the
        # comment was never published while the tool reported success.
        page = FakePage(
            signals=signals(editors=1),
            units=0,
            submit="no_submit_control",
        )
        with navigated():
            result = await actions(page).comment_on_post(
                PERMALINK, "hello", confirm_comment=True
            )
        assert result["status"] == "submit_unavailable"
        assert result["acted"] is False
        assert result["retry_safe"] is True
        assert "clear" in page.calls


class TestRepost:
    """The repost flow, where the wrong index publishes to your own feed."""

    async def test_without_confirmation_no_menu_is_opened(self) -> None:
        page = FakePage(signals=signals())
        with navigated():
            result = await actions(page).repost_post(PERMALINK, confirm_repost=False)
        assert result["status"] == "confirmation_required"
        assert result["acted"] is False
        assert "open_repost" not in page.calls
        assert "pin" not in page.calls

    async def test_a_post_with_no_repost_control_refuses(self) -> None:
        page = FakePage(signals=signals(repost_opener=False))
        with navigated():
            result = await actions(page).repost_post(PERMALINK, confirm_repost=True)
        assert result["status"] == "repost_unavailable"
        assert "open_repost" not in page.calls

    async def test_a_bare_repost_takes_the_first_item_and_confirms_on_change(
        self,
    ) -> None:
        page = FakePage(
            signals=[
                signals(counts=["12", "3"]),
                signals(counts=["12", "3"]),
                signals(counts=["12", "4"]),
            ],
            open_repost="clicked",
            repost_menu={"menus": 1, "items": 2, "layout": "popover"},
            pick_repost=True,
        )
        with navigated():
            result = await actions(page).repost_post(PERMALINK, confirm_repost=True)
        assert result["status"] == "reposted"
        assert result["acted"] is True
        assert result["retry_safe"] is False
        # The current popover puts the immediate repost second.
        assert page.picked_repost["expected"] == 2
        assert page.picked_repost["index"] == 1
        assert page.picked_repost["layout"] == "popover"

    async def test_a_legacy_menu_takes_the_first_item_for_bare_repost(self) -> None:
        page = FakePage(
            signals=[
                signals(counts=["12", "3"]),
                signals(counts=["12", "3"]),
                signals(counts=["12", "4"]),
            ],
            open_repost="clicked",
            repost_menu={"menus": 1, "items": 2, "layout": "menu"},
            pick_repost=True,
        )
        with navigated():
            result = await actions(page).repost_post(PERMALINK, confirm_repost=True)
        assert result["status"] == "reposted"
        assert page.picked_repost["index"] == 0
        assert page.picked_repost["layout"] == "menu"

    async def test_counts_that_never_change_leave_the_repost_unconfirmed(self) -> None:
        page = FakePage(
            signals=signals(counts=["12", "3"]),
            open_repost="clicked",
            repost_menu={"menus": 1, "items": 2, "layout": "popover"},
            pick_repost=True,
        )
        with navigated():
            result = await actions(page).repost_post(PERMALINK, confirm_repost=True)
        assert result["status"] == "repost_unconfirmed"
        assert result["acted"] is False
        assert result["retry_safe"] is False

    @pytest.mark.parametrize("offered", [1, 3])
    async def test_a_menu_of_the_wrong_size_clicks_nothing(self, offered: int) -> None:
        page = FakePage(
            signals=signals(),
            open_repost="clicked",
            repost_menu={"menus": 1, "items": offered},
        )
        with navigated():
            result = await actions(page).repost_post(PERMALINK, confirm_repost=True)
        assert result["status"] == "repost_menu_changed"
        assert result["acted"] is False
        assert "pick_repost" not in page.calls

    async def test_commentary_pins_the_dialog_and_confirms_on_count_change(
        self,
    ) -> None:
        page = FakePage(
            signals=[
                signals(counts=["12", "3"]),
                signals(counts=["12", "3"]),
                signals(counts=["12", "4"]),
            ],
            open_repost="clicked",
            repost_menu={"menus": 1, "items": 2, "layout": "popover"},
            pick_repost=True,
            submit="submitted",
        )
        with navigated():
            result = await actions(page).repost_post(
                PERMALINK, confirm_repost=True, commentary="Worth a read"
            )
        assert result["status"] == "reposted"
        assert result["acted"] is True
        assert result["retry_safe"] is False
        assert page.picked_repost["expected"] == 2
        assert page.picked_repost["index"] == 0
        assert page.picked_repost["layout"] == "popover"
        assert page.pin_editor_scope is page.dialog_handle
        assert "units" not in page.calls
        assert "pin_dialog" in page.calls

    async def test_commentary_url_change_still_pins_the_composer_dialog(
        self,
    ) -> None:
        page = FakePage(
            signals=[
                signals(counts=["12", "3"]),
                signals(counts=["12", "3"]),
                signals(counts=["12", "4"]),
            ],
            open_repost="clicked",
            repost_menu={"menus": 1, "items": 2, "layout": "popover"},
            pick_repost=True,
            submit="submitted",
        )
        page.url = "https://www.linkedin.com/sharing/compose"
        with navigated() as navigate:
            result = await actions(page).repost_post(
                PERMALINK, confirm_repost=True, commentary="Worth a read"
            )
        assert result["status"] == "reposted"
        assert result["acted"] is True
        assert page.pin_editor_scope is page.dialog_handle
        assert "pin_dialog" in page.calls
        assert navigate.await_count == 1

    async def test_commentary_does_not_treat_text_in_the_source_post_as_proof(
        self,
    ) -> None:
        page = FakePage(
            signals=signals(counts=["12", "3"]),
            open_repost="clicked",
            repost_menu={"menus": 1, "items": 2, "layout": "popover"},
            pick_repost=True,
            units=[0, 1],
            submit="submitted",
        )
        with navigated():
            result = await actions(page).repost_post(
                PERMALINK, confirm_repost=True, commentary="Worth a read"
            )
        assert result["status"] == "repost_unconfirmed"
        assert result["acted"] is False
        assert result["retry_safe"] is False
        assert "units" not in page.calls

    async def test_two_visible_dialogs_type_nothing(self) -> None:
        page = FakePage(
            signals=signals(),
            open_repost="clicked",
            repost_menu={"menus": 1, "items": 2, "layout": "popover"},
            pick_repost=True,
            dialog_pinned=False,
        )
        with navigated():
            result = await actions(page).repost_post(
                PERMALINK, confirm_repost=True, commentary="Worth a read"
            )
        assert result["status"] == "repost_composer_unavailable"
        assert "type" not in page.calls
        assert "pin_editor" not in page.calls

    async def test_a_composer_that_never_opens_types_nothing(self) -> None:
        page = FakePage(
            signals=signals(),
            open_repost="clicked",
            repost_menu={"menus": 1, "items": 2, "layout": "popover"},
            pick_repost=True,
        )
        page.dialog_wait.side_effect = TimeoutError("no dialog")
        with navigated():
            result = await actions(page).repost_post(
                PERMALINK, confirm_repost=True, commentary="Worth a read"
            )
        assert result["status"] == "repost_composer_unavailable"
        assert "type" not in page.calls

    async def test_an_opener_that_does_not_expand_clicks_nothing(self) -> None:
        page = FakePage(signals=signals(), open_repost="not_expanded")
        with navigated():
            result = await actions(page).repost_post(PERMALINK, confirm_repost=True)
        assert result["status"] == "repost_unavailable"
        assert "pick_repost" not in page.calls

    async def test_a_disabled_repost_control_refuses(self) -> None:
        page = FakePage(signals=signals(), open_repost="disabled")
        with navigated():
            result = await actions(page).repost_post(PERMALINK, confirm_repost=True)
        assert result["status"] == "repost_unavailable"
        assert "repost_menu" not in page.calls
