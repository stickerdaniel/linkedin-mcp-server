"""Tests for the invitation-action owner.

The write gate is what most of these hold: the invite deeplink may only be
opened to submit once LinkedIn has exposed the vanityName invite anchor, and
the one other thing allowed to open it — the note-quota probe — never clicks
a primary button. Every case that reaches a decision drives the real
classifier from structural signals; no case reads a label.

``tests/test_action_signals_dom.py`` covers the other half, where the
programs run against a real DOM in four label sets. Here ``page.evaluate`` is
a mock, so the JS never executes and the signals are supplied directly.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from patchright.async_api import TimeoutError as PlaywrightTimeoutError

from linkedin_mcp_server.scraping.connection import ActionSignals
from linkedin_mcp_server.scraping.connection_actions import ConnectionActions
from linkedin_mcp_server.scraping.navigation import PageNavigator
from linkedin_mcp_server.scraping.session import ScrapingSession

PREMIUM_MESSAGE = (
    "Wysyłaj nieograniczoną liczbę spersonalizowanych zaproszeń dzięki Premium"
)


def _actions(page, read_main_profile: Any = None) -> ConnectionActions:
    """Wire the connection owner the way the facade does.

    The facade hands over one main-profile read and nothing else of the
    person workflow, so the borrow is the whole collaborator surface a test
    has to supply. The default refuses the call: a case that never reads a
    profile should not be able to, and one that does says which texts it
    expects.
    """

    async def unread(_username: str) -> dict[str, Any]:
        raise AssertionError("this case does not read a profile")

    session = ScrapingSession(page)
    return ConnectionActions(
        session,
        PageNavigator(session),
        read_main_profile if read_main_profile is not None else unread,
    )


def _reads(*texts: str) -> AsyncMock:
    """Script the main-profile read: one answer per call, in order.

    A single text answers every call, which is what the states that never
    re-read need. Two or more script the verification re-reads an action
    performs, and a further call raises ``StopIteration`` rather than
    quietly repeating the last page.
    """
    pages = [
        {
            "url": "https://www.linkedin.com/in/testuser/",
            "sections": {"main_profile": text} if text else {},
        }
        for text in texts
    ]
    if len(pages) == 1:
        return AsyncMock(return_value=pages[0])
    return AsyncMock(side_effect=pages)


def _signals(
    invite: bool = False,
    compose: bool = False,
    edit: bool = False,
    labeled_action: bool = False,
    labeled_anchor: bool = False,
    incoming_row: bool = False,
) -> ActionSignals:
    return ActionSignals(
        has_invite_anchor=invite,
        has_compose_anchor_in_action_root=compose,
        has_edit_intro_anchor=edit,
        has_labeled_action_button=labeled_action,
        has_labeled_action_anchor=labeled_anchor,
        has_incoming_action_row=incoming_row,
    )


class TestConnectWithPerson:
    async def test_connectable_navigates_deeplink_and_verifies(self, mock_page):
        """Connect via deeplink: dialog opens, submit succeeds, anchor disappears."""
        text = "Jane\n\n· 3rd\n\nEngineer\n\nConnect\nMore\nAbout\n"
        post_text = "Jane\n\n· 3rd\n\nEngineer\n\nMessage\nPending\nMore\nAbout\n"
        actions = _actions(mock_page, _reads(text, post_text))

        with (
            patch.object(
                actions,
                "_read_action_signals",
                new_callable=AsyncMock,
                side_effect=[_signals(invite=True), _signals()],
            ),
            patch.object(
                PageNavigator,
                "_navigate_to_page",
                new_callable=AsyncMock,
            ) as mock_nav,
            patch.object(
                actions,
                "_dialog_is_open",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch.object(
                actions,
                "_click_dialog_primary_button",
                new_callable=AsyncMock,
                return_value=True,
            ),
        ):
            result = await actions.connect_with_person("testuser")

        assert result["status"] == "connected"
        mock_nav.assert_awaited_once()
        await_args = mock_nav.await_args
        assert await_args is not None
        assert "preload/custom-invite" in await_args.args[0]

    async def test_connectable_send_failed_when_anchor_persists(self, mock_page):
        """Dialog submitted but profile still exposes Connect → send_failed."""
        text = "Jane\n\n· 3rd\n\nEngineer\n\nConnect\nMore\nAbout\n"
        actions = _actions(mock_page, _reads(text, text))

        with (
            patch.object(
                actions,
                "_read_action_signals",
                new_callable=AsyncMock,
                side_effect=[_signals(invite=True), _signals(invite=True)],
            ),
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch.object(
                actions, "_dialog_is_open", new_callable=AsyncMock, return_value=True
            ),
            patch.object(
                actions,
                "_click_dialog_primary_button",
                new_callable=AsyncMock,
                return_value=True,
            ),
        ):
            result = await actions.connect_with_person("testuser")

        assert result["status"] == "send_failed"

    async def test_connectable_no_dialog_returns_connect_unavailable(self, mock_page):
        """Deeplink opened but no dialog appeared → connect_unavailable."""
        text = "Jane\n\n· 3rd\n\nEngineer\n\nConnect\nMore\nAbout\n"
        actions = _actions(mock_page, _reads(text))

        with (
            patch.object(
                actions,
                "_read_action_signals",
                new_callable=AsyncMock,
                return_value=_signals(invite=True),
            ),
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch.object(
                actions, "_dialog_is_open", new_callable=AsyncMock, return_value=False
            ),
            patch.object(actions, "_dismiss_dialog", new_callable=AsyncMock),
        ):
            result = await actions.connect_with_person("testuser")

        assert result["status"] == "connect_unavailable"

    async def test_returns_already_connected_via_anchor(self, mock_page):
        """1st-degree detected via /messaging/compose anchor."""
        text = "Collin\n\n· 1st\n\nEngineer\n\nMessage\nMore\nAbout\n"
        actions = _actions(mock_page, _reads(text))

        with patch.object(
            actions,
            "_read_action_signals",
            new_callable=AsyncMock,
            return_value=_signals(compose=True),
        ):
            result = await actions.connect_with_person("testuser")

        assert result["status"] == "already_connected"

    async def test_returns_self_profile_via_edit_intro_anchor(self, mock_page):
        """Editing-your-own-profile anchor blocks connect attempts."""
        actions = _actions(mock_page, _reads("Daniel\n\nEdit profile\n"))

        with patch.object(
            actions,
            "_read_action_signals",
            new_callable=AsyncMock,
            return_value=_signals(edit=True),
        ):
            result = await actions.connect_with_person("testuser")

        assert result["status"] == "connect_unavailable"
        assert "own profile" in result["message"]

    async def test_connect_via_more_menu(self, mock_page):
        """Follow-primary profile with Connect under More: detection sees
        no invite anchor initially, _open_more_menu surfaces it, deeplink
        fires."""
        # Pre-More: Follow primary, Connect hidden under the More dropdown.
        pre = "Christian\n\n· 2nd\n\nFounder\n\nFollow\nMessage\nMore\n"
        post = "Christian\n\n· 2nd\n\nFounder\n\nMessage\nPending\nMore\n"
        actions = _actions(mock_page, _reads(pre, post))

        with (
            patch.object(
                actions,
                "_read_action_signals",
                new_callable=AsyncMock,
                # 1st: follow_only (compose+labeled, no invite).
                # 2nd: post-More reread reveals invite anchor.
                # 3rd: post-deeplink verification — invite anchor gone.
                side_effect=[
                    _signals(compose=True, labeled_action=True),
                    _signals(invite=True, compose=True, labeled_action=True),
                    _signals(),
                ],
            ),
            patch.object(
                actions,
                "_open_more_menu",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_open_more,
            patch.object(
                PageNavigator, "_navigate_to_page", new_callable=AsyncMock
            ) as mock_nav,
            patch.object(
                actions,
                "_dialog_is_open",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch.object(
                actions,
                "_click_dialog_primary_button",
                new_callable=AsyncMock,
                return_value=True,
            ),
        ):
            result = await actions.connect_with_person("testuser")

        assert result["status"] == "connected"
        mock_open_more.assert_awaited_once()
        # Deeplink fired exactly once.
        assert mock_nav.await_count == 1
        await_args = mock_nav.await_args
        assert await_args is not None
        assert "preload/custom-invite" in await_args.args[0]

    async def test_follow_only_after_more_does_not_send(self, mock_page):
        """Pending or genuinely follow-only profile: invite anchor never
        appears even after More-menu open. Critical write-gate guardrail —
        no deeplink fires, no connection request goes out."""
        text = "Public Figure\n\n· 3rd+\n\nCEO\n\nFollow\nMessage\nMore\n"
        actions = _actions(mock_page, _reads(text))

        with (
            patch.object(
                actions,
                "_read_action_signals",
                new_callable=AsyncMock,
                # Both reads (initial + post-More) show no invite anchor.
                side_effect=[
                    _signals(compose=True, labeled_action=True),
                    _signals(compose=True, labeled_action=True),
                ],
            ),
            patch.object(
                actions,
                "_open_more_menu",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_open_more,
            patch.object(
                PageNavigator, "_navigate_to_page", new_callable=AsyncMock
            ) as mock_nav,
            patch.object(
                actions,
                "_submit_invite_dialog",
                new_callable=AsyncMock,
                # A successful submit, so a gate that stopped holding reports
                # the deeplink it fired rather than crashing on the mock.
                return_value=(True, False, None),
            ) as mock_submit,
        ):
            result = await actions.connect_with_person("testuser")

        assert result["status"] == "connect_unavailable"
        assert result.get("note_sent") is False or "note_sent" not in result
        mock_open_more.assert_awaited_once()
        # Critical: deeplink must NOT fire and dialog must NOT be submitted.
        mock_nav.assert_not_awaited()
        mock_submit.assert_not_awaited()

    async def test_follow_only_with_note_reports_note_limit_from_deeplink_probe(
        self, mock_page
    ):
        """A requested note may reveal Premium quota without submitting."""
        text = "Public Figure\n\n· 3rd+\n\nCEO\n\nFollow\nMessage\nMore\n"
        actions = _actions(mock_page, _reads(text))

        with (
            patch.object(
                actions,
                "_read_action_signals",
                new_callable=AsyncMock,
                side_effect=[
                    _signals(compose=True, labeled_action=True),
                    _signals(compose=True, labeled_action=True),
                ],
            ),
            patch.object(
                actions,
                "_open_more_menu",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch.object(
                PageNavigator, "_navigate_to_page", new_callable=AsyncMock
            ) as mock_nav,
            patch.object(
                actions,
                "_probe_invite_note_limit",
                new_callable=AsyncMock,
                return_value=PREMIUM_MESSAGE,
            ) as mock_probe,
            patch.object(
                actions,
                "_submit_invite_dialog",
                new_callable=AsyncMock,
                # A successful submit, so a gate that stopped holding reports
                # the deeplink it fired rather than crashing on the mock.
                return_value=(True, False, None),
            ) as mock_submit,
        ):
            result = await actions.connect_with_person("testuser", note="Hello")

        assert result["status"] == "custom_note_limit_reached"
        assert result["message"] == PREMIUM_MESSAGE
        assert result["note_sent"] is False
        mock_nav.assert_awaited_once()
        mock_probe.assert_awaited_once()
        mock_submit.assert_not_awaited()

    async def test_more_menu_unavailable_does_not_send(self, mock_page):
        """Action root present but no More button (unusual but possible):
        _open_more_menu returns False, no retry, no deeplink fires."""
        text = "Public Figure\n\n· 3rd+\n\nCEO\n\nFollow\nMessage\n"
        actions = _actions(mock_page, _reads(text))

        with (
            patch.object(
                actions,
                "_read_action_signals",
                new_callable=AsyncMock,
                return_value=_signals(compose=True, labeled_action=True),
            ),
            patch.object(
                actions,
                "_open_more_menu",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch.object(
                PageNavigator, "_navigate_to_page", new_callable=AsyncMock
            ) as mock_nav,
            patch.object(
                actions,
                "_submit_invite_dialog",
                new_callable=AsyncMock,
                # A successful submit, so a gate that stopped holding reports
                # the deeplink it fired rather than crashing on the mock.
                return_value=(True, False, None),
            ) as mock_submit,
        ):
            result = await actions.connect_with_person("testuser")

        assert result["status"] == "connect_unavailable"
        mock_nav.assert_not_awaited()
        mock_submit.assert_not_awaited()

    async def test_returns_pending(self, mock_page):
        """Profile with a pending invitation: detected via labeled <a> in
        the action root. Returns status='pending' without firing the
        deeplink (LinkedIn would only show 'already invited' anyway)."""
        text = "Frank\n\n· 3rd\n\nFounder\n\nMessage\nPending\nMore\n"
        actions = _actions(mock_page, _reads(text))

        with (
            patch.object(
                actions,
                "_read_action_signals",
                new_callable=AsyncMock,
                return_value=_signals(compose=True, labeled_anchor=True),
            ),
            patch.object(
                PageNavigator, "_navigate_to_page", new_callable=AsyncMock
            ) as mock_nav,
            patch.object(
                actions,
                "_submit_invite_dialog",
                new_callable=AsyncMock,
                # A successful submit, so a gate that stopped holding reports
                # the deeplink it fired rather than crashing on the mock.
                return_value=(True, False, None),
            ) as mock_submit,
            patch.object(
                actions, "_open_more_menu", new_callable=AsyncMock
            ) as mock_open_more,
            patch.object(
                actions, "_click_incoming_accept", new_callable=AsyncMock
            ) as mock_accept,
        ):
            result = await actions.connect_with_person("testuser")

        assert result["status"] == "pending"
        # No write-path side effects, and no action taken on the invitation
        # already out: withdrawing it is what the Pending control does.
        mock_nav.assert_not_awaited()
        mock_submit.assert_not_awaited()
        mock_open_more.assert_not_awaited()
        mock_accept.assert_not_awaited()

    async def test_returns_incoming_request_accepted(self, mock_page):
        """Structural detection + structural accept click, German locale."""
        pre = "Eric\n\n· 2.\n\nAachen\n\nAnnehmen\nIgnorieren\nMehr\nInfo\n"
        post = "Eric\n\n· 1.\n\nAachen\n\nNachricht\nMehr\nInfo\n"
        actions = _actions(mock_page, _reads(pre, post))

        with (
            patch.object(
                actions,
                "_read_action_signals",
                new_callable=AsyncMock,
                side_effect=[
                    _signals(incoming_row=True),
                    _signals(compose=True),
                ],
            ),
            patch.object(
                actions,
                "_click_incoming_accept",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_accept,
            patch.object(
                PageNavigator,
                "_navigate_to_page",
                new_callable=AsyncMock,
            ) as mock_nav,
            patch.object(
                actions,
                "_submit_invite_dialog",
                new_callable=AsyncMock,
            ) as mock_submit,
        ):
            result = await actions.connect_with_person("testuser")

        assert result["status"] == "accepted"
        mock_accept.assert_awaited_once()
        mock_nav.assert_not_awaited()
        mock_submit.assert_not_awaited()

    async def test_incoming_request_send_failed_when_click_fails(self, mock_page):
        """Structural accept click did not land; no locale-text guessing —
        report send_failed without navigating or clicking anything else.

        The owner holds no click-by-text helper to patch, so the claim is
        made against the page: a text fallback would have to build a
        locator, and nothing here builds one.
        """
        pre = "Eric\n\n· 2.\n\nAachen\n\nAnnehmen\nIgnorieren\nMehr\nInfo\n"
        actions = _actions(mock_page, _reads(pre))

        with (
            patch.object(
                actions,
                "_read_action_signals",
                new_callable=AsyncMock,
                return_value=_signals(incoming_row=True),
            ),
            patch.object(
                actions,
                "_click_incoming_accept",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch.object(
                PageNavigator,
                "_navigate_to_page",
                new_callable=AsyncMock,
            ) as mock_nav,
        ):
            result = await actions.connect_with_person("testuser")

        assert result["status"] == "send_failed"
        mock_nav.assert_not_awaited()
        mock_page.locator.assert_not_called()

    async def test_incoming_request_send_failed_when_no_first_degree(self, mock_page):
        """Accept clicked but profile never transitions to 1st-degree."""
        pre = "Eric\n\n· 2.\n\nAachen\n\nAnnehmen\nIgnorieren\nMehr\nInfo\n"
        actions = _actions(mock_page, _reads(pre, pre, pre))

        with (
            patch.object(
                actions,
                "_read_action_signals",
                new_callable=AsyncMock,
                return_value=_signals(incoming_row=True),
            ),
            patch.object(
                actions,
                "_click_incoming_accept",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "linkedin_mcp_server.scraping.connection_actions.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await actions.connect_with_person("testuser")

        assert result["status"] == "send_failed"

    async def test_incoming_request_accepted_on_settle_retry(self, mock_page):
        """The first post-click read still renders the old top card;
        the settle retry sees the 1st-degree state and reports accepted."""
        pre = "Eric\n\n· 2.\n\nAachen\n\nAnnehmen\nIgnorieren\nMehr\nInfo\n"
        post = "Eric\n\n· 1.\n\nAachen\n\nNachricht\nMehr\nInfo\n"
        actions = _actions(mock_page, _reads(pre, pre, post))

        with (
            patch.object(
                actions,
                "_read_action_signals",
                new_callable=AsyncMock,
                side_effect=[
                    _signals(incoming_row=True),
                    _signals(incoming_row=True),
                    _signals(compose=True),
                ],
            ),
            patch.object(
                actions,
                "_click_incoming_accept",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "linkedin_mcp_server.scraping.connection_actions.asyncio.sleep",
                new_callable=AsyncMock,
            ) as mock_sleep,
        ):
            result = await actions.connect_with_person("testuser")

        assert result["status"] == "accepted"
        mock_sleep.assert_awaited_once()

    async def test_returns_unavailable_when_no_signals_and_text(self, mock_page):
        """No structural signals, no actionable text → connect_unavailable."""
        text = "Public Figure\n\n· 3rd+\n\nCEO\n\nFollow\nMore\nAbout\n"
        actions = _actions(mock_page, _reads(text))

        with (
            patch.object(
                actions,
                "_read_action_signals",
                new_callable=AsyncMock,
                return_value=_signals(),
            ),
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch.object(
                actions, "_dialog_is_open", new_callable=AsyncMock, return_value=False
            ),
            patch.object(actions, "_dismiss_dialog", new_callable=AsyncMock),
        ):
            result = await actions.connect_with_person("testuser")

        # follow_only path goes through deeplink; no dialog opens → unavailable
        assert result["status"] == "connect_unavailable"

    async def test_returns_unavailable_on_empty_page(self, mock_page):
        actions = _actions(mock_page, _reads(""))

        result = await actions.connect_with_person("testuser")

        assert result["status"] == "unavailable"

    async def test_normalizes_before_its_own_downstream_use(self, mock_page):
        """The traversal case cannot see this one.

        The person workflow normalizes too, so removing this workflow's own
        call still raises on "../../feed". A full URL is what separates
        them: the read would succeed while the invite deeplink and the
        action-signal selectors kept receiving the URL where they expect
        the vanity.
        """
        read = _reads("text")
        actions = _actions(mock_page, read)
        seen: list[str] = []

        with (
            patch.object(
                actions,
                "_read_action_signals",
                new_callable=AsyncMock,
                side_effect=lambda username: seen.append(username) or _signals(),
            ),
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
        ):
            await actions.connect_with_person(
                "https://de.linkedin.com/in/williamhgates"
            )

        assert seen == ["williamhgates"]
        read.assert_awaited_once_with("williamhgates")


class TestInviteDialog:
    async def test_premium_upsell_message_reads_linkedin_dialog_text(self, mock_page):
        """Premium upsell detection returns LinkedIn's raw dialog text."""
        actions = _actions(mock_page)
        premium_link = MagicMock()
        premium_link.wait_for = AsyncMock(return_value=None)
        premium_link.is_visible = AsyncMock(return_value=True)
        premium_link.inner_text = AsyncMock(return_value="fallback")
        premium_link.first = premium_link
        mock_page.locator.return_value = premium_link
        mock_page.evaluate = AsyncMock(return_value=PREMIUM_MESSAGE)

        result = await actions._get_premium_upsell_message(timeout=1234)

        assert result == PREMIUM_MESSAGE
        mock_page.locator.assert_called_once_with(
            'dialog[open] a[href*="/premium/"], [role="dialog"] a[href*="/premium/"]'
        )
        premium_link.wait_for.assert_awaited_once_with(state="visible", timeout=1234)

    async def test_reports_premium_after_add_note(self, mock_page):
        """Add-note Premium upsell is a note-limit block, not no-dialog."""
        actions = _actions(mock_page)
        textarea = MagicMock()
        textarea.count = AsyncMock(return_value=0)
        add_note_button = MagicMock()
        add_note_button.click = AsyncMock(return_value=None)
        buttons = MagicMock()
        buttons.count = AsyncMock(return_value=3)
        buttons.nth.return_value = add_note_button

        def locator_for(selector: str):
            return textarea if "textarea" in selector else buttons

        mock_page.locator.side_effect = locator_for
        mock_page.wait_for_selector = AsyncMock(
            side_effect=PlaywrightTimeoutError("textarea timeout")
        )

        with (
            patch.object(
                actions, "_dialog_is_open", new_callable=AsyncMock, return_value=True
            ),
            patch.object(
                actions,
                "_get_premium_upsell_message",
                new_callable=AsyncMock,
                return_value=PREMIUM_MESSAGE,
            ) as mock_message,
            patch.object(
                actions, "_dismiss_dialog", new_callable=AsyncMock
            ) as mock_dismiss,
        ):
            result = await actions._submit_invite_dialog("Hello")

        assert result == (False, False, PREMIUM_MESSAGE)
        add_note_button.click.assert_awaited_once()
        mock_message.assert_awaited_once()
        mock_dismiss.assert_awaited_once()

    async def test_reports_premium_after_send_click_failure(self, mock_page):
        """Premium upsell intercepting the Send click is a note-limit block.

        When LinkedIn swaps the invite dialog for the Premium upsell at the
        moment of submit, the original primary button is detached or pointer-
        event covered, so ``_click_dialog_primary_button`` and the keyboard
        fallback both fail. Without the post-click upsell probe the caller
        would dismiss the dialog and report ``connect_unavailable`` even
        though LinkedIn's raw quota message is sitting in the visible modal.
        """
        actions = _actions(mock_page)

        # Textarea already exposed so the reveal/fill branch succeeds and the
        # test focuses on the post-submit failure path.
        textarea = MagicMock()
        textarea.count = AsyncMock(return_value=1)
        textarea.first = textarea
        textarea.fill = AsyncMock()

        buttons = MagicMock()
        buttons.count = AsyncMock(return_value=2)
        primary_button = MagicMock()
        primary_button.focus = AsyncMock()
        buttons.nth.return_value = primary_button

        def locator_for(selector: str):
            return textarea if "textarea" in selector else buttons

        mock_page.locator.side_effect = locator_for
        mock_page.keyboard = MagicMock()
        mock_page.keyboard.press = AsyncMock()

        message = "You're out of free custom notes. Bypass the limit with Premium..."

        with (
            patch.object(
                actions,
                "_dialog_is_open",
                new_callable=AsyncMock,
                # First call: dialog open at entry. Second call: still open
                # after the keyboard fallback, so sent remains False.
                side_effect=[True, True],
            ),
            patch.object(
                actions,
                "_fill_dialog_textarea",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch.object(
                actions,
                "_click_dialog_primary_button",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch.object(
                actions,
                "_get_premium_upsell_message",
                new_callable=AsyncMock,
                return_value=message,
            ) as mock_message,
            patch.object(
                actions, "_dismiss_dialog", new_callable=AsyncMock
            ) as mock_dismiss,
        ):
            result = await actions._submit_invite_dialog("Hello")

        assert result == (False, False, message)
        mock_message.assert_awaited_once()
        mock_dismiss.assert_awaited_once()

    async def test_reports_premium_after_an_accepted_send_click(self, mock_page):
        """A Send click that succeeds is not yet a note that was delivered.

        LinkedIn accepts the click and then swaps the invite dialog for the
        quota upsell, so the only evidence that the note went nowhere is the
        modal standing open afterwards. Reporting this as a send would tell
        the caller a note reached a member who never got one, and the two
        earlier upsell probes cannot see it: both sit on failure paths.
        """
        actions = _actions(mock_page)

        textarea = MagicMock()
        textarea.count = AsyncMock(return_value=1)
        textarea.first = textarea
        textarea.fill = AsyncMock()
        mock_page.locator.return_value = textarea
        mock_page.wait_for_selector = AsyncMock()

        with (
            patch.object(
                actions, "_dialog_is_open", new_callable=AsyncMock, return_value=True
            ),
            patch.object(
                actions,
                "_fill_dialog_textarea",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch.object(
                actions,
                "_click_dialog_primary_button",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch.object(
                actions,
                "_get_premium_upsell_message",
                new_callable=AsyncMock,
                return_value=PREMIUM_MESSAGE,
            ) as mock_message,
            patch.object(
                actions, "_dismiss_dialog", new_callable=AsyncMock
            ) as mock_dismiss,
        ):
            result = await actions._submit_invite_dialog("Hello")

        assert result == (False, False, PREMIUM_MESSAGE)
        mock_message.assert_awaited_once()
        mock_dismiss.assert_awaited_once()
        # The close wait belongs to the delivered path, which this is not.
        mock_page.wait_for_selector.assert_not_awaited()

    async def test_the_quota_probe_never_clicks_the_primary_button(self, mock_page):
        """The probe opens the note editor and touches nothing else.

        It runs only on profiles the write gate already refused, so a click
        on the dialog's *last* button would send the invitation this workflow
        decided not to send. The index ``btn_count - 2`` is the whole of that
        guarantee, and nothing else held it: every case that drives the probe
        through ``connect_with_person`` replaces it wholesale.
        """
        actions = _actions(mock_page)
        clicks: list[int] = []

        def button_at(index: int):
            button = MagicMock()

            async def click(*_args, **_kwargs):
                clicks.append(index)

            button.click = AsyncMock(side_effect=click)
            return button

        # The legacy three-button invite dialog: dismiss, "Add a note", Send.
        buttons = MagicMock()
        buttons.count = AsyncMock(return_value=3)
        buttons.nth = MagicMock(side_effect=button_at)
        textarea = MagicMock()
        textarea.count = AsyncMock(return_value=0)

        def locator_for(selector: str):
            return textarea if "textarea" in selector else buttons

        mock_page.locator.side_effect = locator_for
        mock_page.wait_for_selector = AsyncMock()

        with (
            patch.object(
                actions, "_dialog_is_open", new_callable=AsyncMock, return_value=True
            ),
            patch.object(
                actions,
                "_get_premium_upsell_message",
                new_callable=AsyncMock,
                # Nothing before the note editor opens, the quota block after.
                side_effect=[None, PREMIUM_MESSAGE],
            ),
            patch.object(
                actions, "_dismiss_dialog", new_callable=AsyncMock
            ) as mock_dismiss,
        ):
            message = await actions._probe_invite_note_limit()

        assert message == PREMIUM_MESSAGE
        assert clicks == [1]
        mock_dismiss.assert_awaited_once()

    async def test_handles_two_button_gating_dialog(self, mock_page):
        """Two-button "Add a note to your invitation?" gating dialog (issue
        #455): nth(0) is "Add a note", nth(1) is "Send without a note".

        Asserts the secondary-button click that reveals the textarea fires
        even with btn_count == 2 (legacy guard required >= 3 and skipped
        the click, leaving the textarea unmounted)."""
        actions = _actions(mock_page)

        # Track each button click so we can assert the "Add a note" path
        # was taken to reveal the textarea.
        clicks: list[int] = []

        textarea_visible = {"value": False}

        # Two button locators inside the gating dialog: nth(0) "Add a
        # note" reveals the textarea, nth(1) "Send without a note".
        button_locators = [MagicMock(), MagicMock()]
        for idx, btn in enumerate(button_locators):

            def make_click(i: int):
                async def _click(*args, **kwargs):
                    clicks.append(i)
                    if i == 0:
                        textarea_visible["value"] = True
                    return None

                return _click

            btn.click = AsyncMock(side_effect=make_click(idx))
            btn.focus = AsyncMock()

        button_collection = MagicMock()
        button_collection.count = AsyncMock(return_value=2)
        button_collection.nth = MagicMock(side_effect=lambda i: button_locators[i])

        textarea_locator = MagicMock()
        textarea_locator.count = AsyncMock(
            side_effect=lambda: 1 if textarea_visible["value"] else 0
        )
        textarea_locator.first = textarea_locator
        textarea_locator.fill = AsyncMock()

        # Route page.locator() calls by selector — buttons vs textarea —
        # so the gating dialog's button collection is distinguishable
        # from the textarea probe.
        def locator_router(selector: str):
            if "textarea" in selector:
                return textarea_locator
            return button_collection

        mock_page.locator = MagicMock(side_effect=locator_router)
        mock_page.wait_for_selector = AsyncMock()
        mock_page.keyboard = MagicMock()
        mock_page.keyboard.press = AsyncMock()

        with (
            patch.object(
                actions, "_dialog_is_open", new_callable=AsyncMock, return_value=True
            ),
            patch.object(
                actions,
                "_get_premium_upsell_message",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            (
                submitted,
                note_sent,
                note_limit_message,
            ) = await actions._submit_invite_dialog("Hi from a test")

        assert submitted is True
        assert note_sent is True
        assert note_limit_message is None
        # Clicked "Add a note" (index 0) to reveal the textarea, then the
        # primary button (index 1) to send.
        assert clicks == [0, 1]
        textarea_locator.fill.assert_awaited_once()
