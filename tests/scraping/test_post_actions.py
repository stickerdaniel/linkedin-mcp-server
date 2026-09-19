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

from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from patchright.async_api import Page

import linkedin_mcp_server.scraping.post_actions as post_actions
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
    "insert": post_actions.INSERT_TEXT_JS,
    "submit": post_actions.SUBMIT_EDITOR_JS,
    "units": post_actions.COUNT_TEXT_UNITS_JS,
}


def signals(
    *,
    root: bool = True,
    bar: bool = True,
    pressed: bool = False,
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
        "reactPressed": pressed if bar else None,
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


class FakePage:
    """A page double that answers each extractor program from a script.

    Every keyword takes either one answer for all calls or a list consumed in
    order, so a flow that re-reads state can be given a transition. ``calls``
    records which programs ran, which is how a test asserts that nothing was
    clicked.
    """

    def __init__(self, *, pinned: bool = True, **answers: Any):
        self.url = POST_URL
        self.calls: list[str] = []
        self._answers = {name: answers.get(name) for name in PROGRAMS}
        self._sequences = {
            name: list(value) if isinstance(value, list) else None
            for name, value in self._answers.items()
        }
        self._last: dict[str, Any] = {}
        self.handle = FakeHandle(pinned=pinned)
        self.keyboard = MagicMock()
        self.keyboard.press = AsyncMock()
        self.wait_for_selector = AsyncMock()
        self.dialog_wait = AsyncMock()

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
        assert page.picked_reaction == {"expected": 6, "index": 5}
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
        assert "insert" not in page.calls
        assert "submit" not in page.calls
        assert "pin" not in page.calls

    async def test_a_post_with_no_comment_editor_refuses(self) -> None:
        page = FakePage(signals=signals(editors=0))
        with navigated():
            result = await actions(page).comment_on_post(
                PERMALINK, "hello", confirm_comment=True
            )
        assert result["status"] == "comment_box_unavailable"
        assert "insert" not in page.calls

    async def test_two_comment_editors_refuse_rather_than_guess(self) -> None:
        page = FakePage(signals=signals(editors=2))
        with navigated():
            result = await actions(page).comment_on_post(
                PERMALINK, "hello", confirm_comment=True
            )
        assert result["status"] == "comment_box_ambiguous"
        assert "insert" not in page.calls

    async def test_a_confirmed_comment_is_typed_submitted_and_verified(self) -> None:
        page = FakePage(
            signals=signals(editors=1),
            units=[0, 1],
            insert="inserted",
            submit="submitted",
        )
        with navigated():
            result = await actions(page).comment_on_post(
                PERMALINK, "Great write-up", confirm_comment=True
            )
        assert result["status"] == "commented"
        assert result["acted"] is True
        assert result["retry_safe"] is False
        assert page.calls.count("insert") == 1
        assert page.calls.count("submit") == 1
        assert page.handle.disposed

    async def test_text_that_never_appears_is_unconfirmed_and_unsafe(self) -> None:
        page = FakePage(
            signals=signals(editors=1),
            units=0,
            insert="inserted",
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
            insert="inserted",
            submit="submitted",
        )
        with navigated():
            result = await actions(page).comment_on_post(
                PERMALINK, "Great write-up", confirm_comment=True
            )
        assert result["status"] == "comment_unconfirmed"

    async def test_an_existing_draft_is_left_untouched(self) -> None:
        page = FakePage(
            signals=signals(editors=1),
            units=0,
            insert="draft_present",
        )
        with navigated():
            result = await actions(page).comment_on_post(
                PERMALINK, "hello", confirm_comment=True
            )
        assert result["status"] == "draft_present"
        assert result["retry_safe"] is True
        assert "submit" not in page.calls

    async def test_two_submit_candidates_refuse_and_stay_retry_safe(self) -> None:
        page = FakePage(
            signals=signals(editors=1),
            units=0,
            insert="inserted",
            submit="ambiguous_submit",
        )
        with navigated():
            result = await actions(page).comment_on_post(
                PERMALINK, "hello", confirm_comment=True
            )
        assert result["status"] == "submit_unavailable"
        assert result["acted"] is False
        # Nothing was dispatched, so calling again cannot double-publish.
        assert result["retry_safe"] is True


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
            signals=[signals(counts=["12", "3"]), signals(counts=["12", "4"])],
            open_repost="clicked",
            repost_menu={"menus": 1, "items": 2},
            pick_repost=True,
        )
        with navigated():
            result = await actions(page).repost_post(PERMALINK, confirm_repost=True)
        assert result["status"] == "reposted"
        assert result["acted"] is True
        assert result["retry_safe"] is False
        # Index 0 is the immediate repost; index 1 opens a composer.
        assert page.picked_repost == {"expected": 2, "index": 0}

    async def test_counts_that_never_change_leave_the_repost_unconfirmed(self) -> None:
        page = FakePage(
            signals=signals(counts=["12", "3"]),
            open_repost="clicked",
            repost_menu={"menus": 1, "items": 2},
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

    async def test_commentary_takes_the_second_item_and_verifies_the_text(self) -> None:
        page = FakePage(
            signals=signals(),
            open_repost="clicked",
            repost_menu={"menus": 1, "items": 2},
            pick_repost=True,
            units=[0, 1],
            insert="inserted",
            submit="submitted",
        )
        with navigated():
            result = await actions(page).repost_post(
                PERMALINK, confirm_repost=True, commentary="Worth a read"
            )
        assert result["status"] == "reposted"
        assert result["acted"] is True
        assert page.picked_repost == {"expected": 2, "index": 1}

    async def test_a_composer_that_never_opens_types_nothing(self) -> None:
        page = FakePage(
            signals=signals(),
            open_repost="clicked",
            repost_menu={"menus": 1, "items": 2},
            pick_repost=True,
        )
        page.dialog_wait.side_effect = TimeoutError("no dialog")
        with navigated():
            result = await actions(page).repost_post(
                PERMALINK, confirm_repost=True, commentary="Worth a read"
            )
        assert result["status"] == "repost_composer_unavailable"
        assert "insert" not in page.calls

    async def test_a_disabled_repost_control_refuses(self) -> None:
        page = FakePage(signals=signals(), open_repost="disabled")
        with navigated():
            result = await actions(page).repost_post(PERMALINK, confirm_repost=True)
        assert result["status"] == "repost_unavailable"
        assert "repost_menu" not in page.calls
