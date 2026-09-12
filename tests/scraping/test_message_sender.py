"""Tests for the browser-UI message sender."""

from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import asyncio
import logging

from patchright.async_api import Error as PatchrightError
from patchright.async_api import TimeoutError as PlaywrightTimeoutError

import pytest

from linkedin_mcp_server.scraping import message_sender as message_sender_module
from linkedin_mcp_server.scraping.message_sender import (
    MessageSender,
    _MESSAGE_COMPOSER_OWNER_JS,
    _MESSAGE_CONFIRMATION_DISPOSE_JS,
    _MESSAGE_CONFIRMATION_PREPARE_JS,
    _MESSAGE_CONFIRMATION_READY_JS,
)
from linkedin_mcp_server.scraping.navigation import PageNavigator
from linkedin_mcp_server.scraping.session import ScrapingSession


def _sender(page) -> MessageSender:
    session = ScrapingSession(page)
    return MessageSender(session, PageNavigator(session))


class TestMessageTargetUrls:
    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            (
                "https://www.linkedin.com/messaging/compose/?recipient=ACoAAB",
                "ACoAAB",
            ),
            (
                "https://de.linkedin.com/messaging/compose/"
                "?recipient=ACoAAB&profileUrn=urn%3Ali%3Afsd_profile%3AACoAAB",
                "ACoAAB",
            ),
            (
                "https://www.linkedin.com/messaging/compose/"
                "?recipient=ACoAAB&recipient=ACoAAB",
                "ACoAAB",
            ),
            ("http://www.linkedin.com/messaging/compose/?recipient=ACoAAB", None),
            ("https://evil.example/messaging/compose/?recipient=ACoAAB", None),
            ("//evil.example/messaging/compose/?recipient=ACoAAB", None),
            ("https://user@www.linkedin.com/messaging/compose/?recipient=ACoAAB", None),
            ("https://www.linkedin.com:444/messaging/compose/?recipient=ACoAAB", None),
            ("https://www.linkedin.com/jobs/?recipient=ACoAAB", None),
            (
                "https://www.linkedin.com/messaging/compose/?recipient=ACoAAB#draft",
                None,
            ),
            ("https://www.linkedin.com/messaging/compose/?recipient=ACoAAB\n", None),
            ("https://www.linkedin.com/messaging/compose/?recipient=", None),
            (
                "https://www.linkedin.com/messaging/compose/"
                "?recipient=ACoAAB&recipient=OTHER",
                None,
            ),
            (
                "https://www.linkedin.com/messaging/compose/"
                "?recipient=ACoAAB&profileUrn=urn%3Ali%3Afsd_profile%3AOTHER",
                None,
            ),
            (
                "https://www.linkedin.com/messaging/compose/?profileUrn=malformed%3Aurn",
                None,
            ),
        ],
    )
    def test_compose_url_requires_one_linkedin_recipient(self, url, expected):
        assert message_sender_module._profile_urn_from_compose_url(url) == expected

    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("https://www.linkedin.com/in/testuser/", "/in/testuser/"),
            ("https://de.linkedin.com/in/testuser/", "/in/testuser/"),
            ("http://www.linkedin.com/in/testuser/", None),
            ("https://evil.example/in/testuser/", None),
            ("https://user@www.linkedin.com/in/testuser/", None),
            ("https://www.linkedin.com:444/in/testuser/", None),
            ("https://www.linkedin.com/in/testuser/edit/intro/", None),
            ("https://www.linkedin.com/in/testuser%2Fedit/", None),
            ("https://www.linkedin.com/in/testuser/?trk=profile", None),
            ("https://www.linkedin.com/in/testuser/#details", None),
        ],
    )
    def test_profile_url_requires_exact_linkedin_profile(self, url, expected):
        assert message_sender_module._profile_path_from_url(url) == expected

    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("https://www.linkedin.com/messaging/compose/", True),
            (
                "https://www.linkedin.com/messaging/compose/?recipient=ACoAAB",
                True,
            ),
            (
                "https://www.linkedin.com/messaging/compose/"
                "?recipient=ACoAAB&recipient=ACoAAB&"
                "profileUrn=urn%3Ali%3Afsd_profile%3AACoAAB",
                True,
            ),
            ("https://de.linkedin.com/messaging/thread/2-abc/", True),
            (
                "https://www.linkedin.com/messaging/thread/2-abc/"
                "?recipient=ACoAAB&profileUrn=urn%3Ali%3Afsd_profile%3AACoAAB",
                True,
            ),
            (
                "https://www.linkedin.com/messaging/compose/?recipient=OTHER",
                False,
            ),
            (
                "https://www.linkedin.com/messaging/compose/"
                "?recipient=ACoAAB&recipient=OTHER",
                False,
            ),
            (
                "https://www.linkedin.com/messaging/compose/?profileUrn=",
                False,
            ),
            (
                "https://www.linkedin.com/messaging/thread/2-abc/"
                "?recipient=ACoAAB&recipient=OTHER",
                False,
            ),
            (
                "https://www.linkedin.com/messaging/thread/2-abc/?profileUrn=",
                False,
            ),
            ("http://www.linkedin.com/messaging/compose/", False),
            ("https://evil.example/messaging/compose/", False),
            ("https://user@www.linkedin.com/messaging/thread/2-abc/", False),
            ("https://www.linkedin.com:444/messaging/compose/", False),
            ("https://www.linkedin.com/messaging/compose/#draft", False),
            ("https://www.linkedin.com/messaging/thread/2-abc%2Fother/", False),
            # Measured live: LinkedIn redirects an existing conversation to a
            # padded base64url id, and the padding reaches the path unescaped.
            (
                "https://www.linkedin.com/messaging/thread/"
                "2-ZDBkMjZiY2UtNjQwYi00NzczLWIxYWYtNTczZTZhZDkzMzQ4XzEwMA==/",
                True,
            ),
            ("https://www.linkedin.com/feed/", False),
        ],
    )
    def test_final_url_requires_safe_messaging_path(self, url, expected):
        assert (
            message_sender_module._message_page_url_is_safe(url, "ACoAAB") is expected
        )


class TestReadProfileMessageTarget:
    async def test_accepts_safe_final_vanity_redirect(self, mock_page):
        mock_page.evaluate = AsyncMock(
            return_value={
                "status": "resolved",
                "pageUrl": "https://www.linkedin.com/in/canonical-user/",
                "displayName": "Test User",
                "composeHrefs": [
                    "/messaging/compose/?recipient=ACoAAB&"
                    "profileUrn=urn%3Ali%3Afsd_profile%3AACoAAB"
                ],
            }
        )

        resolution = await _sender(mock_page)._read_profile_message_target()

        assert resolution.status == "resolved"
        assert resolution.target is not None
        assert resolution.target.profile_path == "/in/canonical-user/"
        assert resolution.target.profile_urn == "ACoAAB"


class TestSendMessage:
    @pytest.mark.parametrize("message", ["", " \t\n"], ids=["empty", "whitespace"])
    async def test_blank_message_is_rejected_before_browser_interaction(
        self, mock_page, message
    ):
        sender = _sender(mock_page)
        keyboard = MagicMock()
        mock_page.keyboard = keyboard

        with patch.object(
            PageNavigator, "_navigate_to_page", new_callable=AsyncMock
        ) as navigate:
            result = await sender.send_message("testuser", message, confirm_send=True)

        # Not `message_unavailable`: that status is about the recipient and
        # tells a caller to move on, while this one is about their own input.
        assert result == {
            "url": "https://www.linkedin.com/in/testuser/",
            "status": "invalid_message",
            "message": "Message must contain non-whitespace characters.",
            "recipient_selected": False,
            "sent": False,
            "retry_safe": True,
        }
        navigate.assert_not_awaited()
        mock_page.evaluate.assert_not_awaited()
        keyboard.type.assert_not_called()
        keyboard.press.assert_not_called()

    @pytest.mark.parametrize(
        "message",
        ["First\nSecond", "First\rSecond", "First\tSecond", "First\x7fSecond"],
        ids=["newline", "carriage-return", "tab", "del"],
    )
    async def test_control_message_is_rejected_before_browser_interaction(
        self, mock_page, message
    ):
        sender = _sender(mock_page)
        mock_page.keyboard = MagicMock(type=AsyncMock(), press=AsyncMock())

        with patch.object(
            PageNavigator, "_navigate_to_page", new_callable=AsyncMock
        ) as navigate:
            result = await sender.send_message("testuser", message, confirm_send=True)

        assert result["status"] == "invalid_message"
        assert result["message"] == (
            "Message must not contain control characters or line breaks."
        )
        assert result["retry_safe"] is True
        navigate.assert_not_awaited()
        mock_page.evaluate.assert_not_awaited()
        mock_page.keyboard.type.assert_not_awaited()
        mock_page.keyboard.press.assert_not_awaited()

    async def test_unavailable_message_action_returns_connection_handoff(
        self, mock_page
    ):
        sender = _sender(mock_page)
        mock_page.keyboard = MagicMock(type=AsyncMock(), press=AsyncMock())

        with (
            patch.object(
                PageNavigator, "_navigate_to_page", new_callable=AsyncMock
            ) as navigate,
            patch.object(ScrapingSession, "check_rate_limit", new_callable=AsyncMock),
            patch.object(
                sender,
                "_read_profile_message_target",
                new_callable=AsyncMock,
                return_value=message_sender_module._ProfileMessageTargetResolution(
                    "unavailable"
                ),
            ),
            patch.object(
                sender, "_wait_for_message_surface", new_callable=AsyncMock
            ) as surface,
            patch.object(
                sender, "_read_message_composer_state", new_callable=AsyncMock
            ) as state,
            patch.object(
                sender,
                "_focus_verified_message_editor",
                new_callable=AsyncMock,
            ) as focus,
            patch.object(
                sender, "_submit_verified_message", new_callable=AsyncMock
            ) as submit,
        ):
            result = await sender.send_message("testuser", "Hello!", confirm_send=True)

        assert result == {
            "url": "https://www.linkedin.com/in/testuser/",
            "status": "message_unavailable",
            "message": (
                "LinkedIn did not expose a normal Message action for this profile. "
                "Use connect_with_person first, then retry only after the connection "
                "request is accepted."
            ),
            "recipient_selected": False,
            "sent": False,
            "retry_safe": True,
        }
        navigate.assert_awaited_once_with("https://www.linkedin.com/in/testuser/")
        surface.assert_not_awaited()
        state.assert_not_awaited()
        focus.assert_not_awaited()
        submit.assert_not_awaited()
        mock_page.keyboard.type.assert_not_awaited()
        mock_page.keyboard.press.assert_not_awaited()

    async def test_unresolved_profile_target_is_not_connection_handoff(self, mock_page):
        sender = _sender(mock_page)
        with (
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch.object(ScrapingSession, "check_rate_limit", new_callable=AsyncMock),
            patch.object(
                sender,
                "_read_profile_message_target",
                new_callable=AsyncMock,
                return_value=message_sender_module._ProfileMessageTargetResolution(
                    "failed"
                ),
            ),
        ):
            result = await sender.send_message("testuser", "Hello!", confirm_send=True)

        assert result["status"] == "recipient_resolution_failed"
        assert "connect_with_person" not in result["message"]
        assert result["retry_safe"] is True

    @staticmethod
    def _target():
        return message_sender_module._ProfileMessageTarget(
            profile_path="/in/testuser/",
            profile_urn="ACoAAB",
            compose_url=(
                "https://www.linkedin.com/messaging/compose/"
                "?recipient=ACoAAB&profileUrn=urn%3Ali%3Afsd_profile%3AACoAAB"
            ),
            display_name="Test User",
        )

    @staticmethod
    def _patch_to_composer(
        sender,
        mock_page,
        *,
        states=None,
        submission="clicked",
        write_result="written",
    ):
        target = TestSendMessage._target()
        mock_page.url = "https://www.linkedin.com/messaging/compose/?recipient=ACoAAB"
        mock_page.keyboard = MagicMock(type=AsyncMock(), press=AsyncMock())
        owner = MagicMock()
        owner.as_element.return_value = owner
        owner.evaluate = AsyncMock(return_value="ready")
        owner.dispose = AsyncMock()
        mock_page.evaluate_handle = AsyncMock(return_value=owner)

        # An empty composer is the ordinary precondition for sending, so a
        # state that says nothing about it means empty. A case about a draft
        # still standing in the editor says `"empty": False` and gets it.
        def with_empty(state):
            if not isinstance(state, dict):
                return state
            return {
                "empty": True,
                "submitCount": 1,
                "submitUsable": True,
                **state,
            }

        if callable(states):
            inner = states

            async def states(*args, **kwargs):
                return with_empty(await inner(*args, **kwargs))
        elif states is not None:
            states = [with_empty(state) for state in states]
        return (
            target,
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch.object(ScrapingSession, "check_rate_limit", new_callable=AsyncMock),
            patch.object(
                sender,
                "_read_profile_message_target",
                new_callable=AsyncMock,
                return_value=message_sender_module._ProfileMessageTargetResolution(
                    "resolved", target
                ),
            ),
            patch.object(
                sender,
                "_wait_for_message_surface",
                new_callable=AsyncMock,
                return_value="composer",
            ),
            patch.object(
                sender,
                "_read_message_composer_state",
                new_callable=AsyncMock,
                side_effect=states or None,
                return_value={
                    "status": "valid",
                    "active": False,
                    "empty": True,
                    "submitCount": 1,
                    "submitUsable": True,
                },
            ),
            patch.object(
                sender,
                "_write_verified_message",
                new_callable=AsyncMock,
                return_value=write_result,
            ),
            patch.object(
                sender,
                "_submit_verified_message",
                new_callable=AsyncMock,
                return_value=submission,
            ),
            patch(
                "linkedin_mcp_server.scraping.message_sender.asyncio.sleep",
                new_callable=AsyncMock,
            ),
            patch.object(
                sender,
                "_prepare_message_confirmation",
                new_callable=AsyncMock,
                return_value="confirmation-token",
            ),
            patch.object(
                sender,
                "_message_send_confirmed",
                new_callable=AsyncMock,
                return_value=True,
            ),
        )

    async def test_dry_run_returns_before_focus_or_text_entry(self, mock_page):
        sender = _sender(mock_page)
        patches = self._patch_to_composer(sender, mock_page)
        with (
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6] as focus,
            patches[7] as submit,
            patches[8],
        ):
            result = await sender.send_message("testuser", "Hello!", confirm_send=False)

        assert result["status"] == "confirmation_required"
        assert result["recipient_selected"] is True
        focus.assert_not_awaited()
        submit.assert_not_awaited()
        mock_page.keyboard.type.assert_not_awaited()

    async def test_rejects_supplied_urn_before_compose_navigation(self, mock_page):
        sender = _sender(mock_page)
        target = self._target()
        with (
            patch.object(
                PageNavigator, "_navigate_to_page", new_callable=AsyncMock
            ) as navigate,
            patch.object(ScrapingSession, "check_rate_limit", new_callable=AsyncMock),
            patch.object(
                sender,
                "_read_profile_message_target",
                new_callable=AsyncMock,
                return_value=message_sender_module._ProfileMessageTargetResolution(
                    "resolved", target
                ),
            ),
        ):
            result = await sender.send_message(
                "testuser",
                "Hello!",
                confirm_send=True,
                profile_urn="OTHER",
            )

        assert result["status"] == "recipient_resolution_failed"
        navigate.assert_awaited_once_with("https://www.linkedin.com/in/testuser/")

    async def test_rejects_foreign_url_recipient_after_navigation(self, mock_page):
        sender = _sender(mock_page)
        patches = self._patch_to_composer(sender, mock_page)
        mock_page.url = "https://www.linkedin.com/messaging/compose/?recipient=OTHER"
        with (
            patches[1],
            patches[2],
            patches[3],
            patches[4] as surface,
            patches[5] as state,
            patches[6] as focus,
            patches[7] as submit,
            patches[8],
            patches[9],
            patches[10],
        ):
            result = await sender.send_message("testuser", "Hello!", confirm_send=True)

        assert result["status"] == "recipient_resolution_failed"
        surface.assert_not_awaited()
        state.assert_not_awaited()
        focus.assert_not_awaited()
        submit.assert_not_awaited()
        mock_page.keyboard.type.assert_not_awaited()

    async def test_rejects_contradictory_url_before_focus(self, mock_page):
        sender = _sender(mock_page)

        async def change_url_after_initial_state(_target):
            mock_page.url = (
                "https://www.linkedin.com/messaging/compose/"
                "?recipient=ACoAAB&recipient=OTHER"
            )
            return {"status": "valid", "active": False}

        patches = self._patch_to_composer(
            sender,
            mock_page,
            states=change_url_after_initial_state,
        )
        with (
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5] as state,
            patches[6] as focus,
            patches[7] as submit,
            patches[8],
            patches[9],
            patches[10],
        ):
            result = await sender.send_message("testuser", "Hello!", confirm_send=True)

        assert result["status"] == "recipient_resolution_failed"
        state.assert_awaited_once()
        focus.assert_not_awaited()
        submit.assert_not_awaited()
        mock_page.keyboard.type.assert_not_awaited()

    async def test_rejects_foreign_url_recipient_before_text_entry(self, mock_page):
        sender = _sender(mock_page)
        state_calls = 0

        async def change_url_during_prefocus_state(_target):
            nonlocal state_calls
            state_calls += 1
            if state_calls == 2:
                mock_page.url = (
                    "https://www.linkedin.com/messaging/compose/?recipient=OTHER"
                )
            return {"status": "valid", "active": state_calls > 1}

        patches = self._patch_to_composer(
            sender,
            mock_page,
            states=change_url_during_prefocus_state,
        )
        with (
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6] as focus,
            patches[7] as submit,
            patches[8],
            patches[9],
            patches[10],
        ):
            result = await sender.send_message("testuser", "Hello!", confirm_send=True)

        assert result["status"] == "recipient_resolution_failed"
        focus.assert_not_awaited()
        submit.assert_not_awaited()
        mock_page.keyboard.type.assert_not_awaited()

    async def test_queryless_route_switch_during_surface_wait_fails_closed(
        self, mock_page
    ):
        sender = _sender(mock_page)
        patches = self._patch_to_composer(sender, mock_page)
        alice_route = "https://www.linkedin.com/messaging/thread/ALICE/"
        bob_route = "https://www.linkedin.com/messaging/thread/BOB/"
        mock_page.url = alice_route

        async def switch_route(_target):
            mock_page.url = bob_route
            return "composer"

        with (
            patches[1],
            patches[2],
            patches[3],
            patches[4] as surface,
            patches[5] as state,
            patches[6] as write,
            patches[7] as submit,
            patches[8],
            patches[9],
            patches[10],
        ):
            surface.side_effect = switch_route
            result = await sender.send_message("testuser", "Hello!", confirm_send=True)

        assert result["status"] == "recipient_resolution_failed"
        assert result["retry_safe"] is True
        assert result["url"] == bob_route
        state.assert_not_awaited()
        write.assert_not_awaited()
        submit.assert_not_awaited()
        mock_page.keyboard.type.assert_not_awaited()
        mock_page.keyboard.press.assert_not_awaited()

    async def test_queryless_route_is_captured_before_owner_resolution(self, mock_page):
        sender = _sender(mock_page)
        patches = self._patch_to_composer(sender, mock_page)
        alice_route = "https://www.linkedin.com/messaging/thread/ALICE/"
        bob_route = "https://www.linkedin.com/messaging/thread/BOB/"
        mock_page.url = alice_route

        async def switch_route(target, *, expected_route):
            assert target == self._target()
            assert expected_route == alice_route
            mock_page.url = bob_route
            return None

        with (
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6] as write,
            patches[7] as submit,
            patches[8],
            patches[9],
            patches[10],
            patch.object(
                sender,
                "_resolve_message_owner",
                new_callable=AsyncMock,
                side_effect=switch_route,
            ) as resolve_owner,
        ):
            result = await sender.send_message("testuser", "Hello!", confirm_send=True)

        assert result["status"] == "recipient_resolution_failed"
        assert result["retry_safe"] is True
        resolve_owner.assert_awaited_once_with(
            self._target(), expected_route=alice_route
        )
        write.assert_not_awaited()
        submit.assert_not_awaited()
        mock_page.keyboard.type.assert_not_awaited()
        mock_page.keyboard.press.assert_not_awaited()

    async def test_rejects_contradictory_url_before_submission(self, mock_page):
        sender = _sender(mock_page)
        patches = self._patch_to_composer(sender, mock_page)

        async def change_url_during_write(message, *, target, owner):
            assert message == "Hello!"
            assert target == self._target()
            assert owner is mock_page.evaluate_handle.return_value
            mock_page.url = (
                "https://www.linkedin.com/messaging/compose/"
                "?recipient=ACoAAB&profileUrn=urn%3Ali%3Afsd_profile%3AOTHER"
            )
            return "invalid"

        with (
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6] as write,
            patches[7] as submit,
            patches[8],
            patches[9],
            patches[10],
        ):
            write.side_effect = change_url_during_write
            result = await sender.send_message("testuser", "Hello!", confirm_send=True)

        assert result["status"] == "recipient_resolution_failed"
        write.assert_awaited_once()
        mock_page.keyboard.type.assert_not_awaited()
        submit.assert_not_awaited()
        mock_page.keyboard.press.assert_not_awaited()

    async def test_refuses_a_composer_that_already_holds_a_draft(self, mock_page):
        """A draft in the editor is not ours to send, and not ours to clear."""
        sender = _sender(mock_page)
        patches = self._patch_to_composer(
            sender,
            mock_page,
            # The recipient check first, then the read taken immediately
            # before focus: that one still finds the author's draft.
            states=[
                {"status": "valid", "active": False},
                {"status": "valid", "active": False, "empty": False},
            ],
        )
        with (
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6] as focus,
            patches[7] as submit,
            patches[8],
            patches[9],
            patches[10],
        ):
            result = await sender.send_message("testuser", "Hello!", confirm_send=True)

        assert result["status"] == "composer_occupied"
        assert result["sent"] is False
        # Nothing is typed, nothing is submitted, and the draft is left where
        # its author put it.
        focus.assert_not_awaited()
        submit.assert_not_awaited()
        mock_page.keyboard.type.assert_not_awaited()
        mock_page.keyboard.press.assert_not_awaited()

    async def test_rejects_recipient_change_before_focus(self, mock_page):
        sender = _sender(mock_page)
        patches = self._patch_to_composer(
            sender,
            mock_page,
            states=[
                {"status": "valid", "active": False},
                {"status": "recipient_mismatch", "active": False},
            ],
        )
        with (
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6] as focus,
            patches[7],
            patches[8],
            patches[9],
            patches[10],
        ):
            result = await sender.send_message("testuser", "Hello!", confirm_send=True)

        assert result["status"] == "compose_interact_failed"
        focus.assert_not_awaited()
        mock_page.keyboard.type.assert_not_awaited()

    async def test_rejects_editor_change_before_text_entry(self, mock_page):
        sender = _sender(mock_page)
        patches = self._patch_to_composer(
            sender,
            mock_page,
            states=[
                {"status": "valid", "active": False},
                {"status": "valid", "active": False},
            ],
            write_result="invalid",
        )
        with (
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
            patches[7],
            patches[8],
            patch.object(
                sender,
                "_prepare_message_confirmation",
                new_callable=AsyncMock,
                return_value=0,
            ),
            patch.object(
                sender,
                "_message_send_confirmed",
                new_callable=AsyncMock,
                return_value=True,
            ),
        ):
            result = await sender.send_message("testuser", "Hello!", confirm_send=True)

        assert result["status"] == "compose_interact_failed"
        mock_page.keyboard.type.assert_not_awaited()

    async def test_missing_owner_is_retryable_before_dispatch(self, mock_page):
        sender = _sender(mock_page)
        patches = self._patch_to_composer(sender, mock_page)
        owner = mock_page.evaluate_handle.return_value
        owner.as_element.return_value = None
        with ExitStack() as stack:
            entered = [stack.enter_context(item) for item in patches[1:]]
            result = await sender.send_message("testuser", "Hello!", confirm_send=True)

        assert result["status"] == "recipient_resolution_failed"
        assert result["sent"] is False
        assert result["retry_safe"] is True
        entered[6].assert_not_awaited()
        owner.dispose.assert_awaited_once_with()

    async def test_rejects_ambiguous_submit_after_text_entry(self, mock_page):
        sender = _sender(mock_page)
        patches = self._patch_to_composer(sender, mock_page, submission="invalid")
        with (
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6] as write,
            patches[7],
            patches[8],
            patches[9],
            patches[10],
        ):
            result = await sender.send_message("testuser", "Hello!", confirm_send=True)

        assert result["status"] == "send_unavailable"
        assert result["retry_safe"] is True
        write.assert_awaited_once()
        mock_page.keyboard.type.assert_not_awaited()
        mock_page.keyboard.press.assert_not_awaited()

    async def test_disabled_pinned_submit_cleans_before_retryable_failure(
        self, mock_page
    ):
        sender = _sender(mock_page)
        patches = self._patch_to_composer(sender, mock_page)
        owner = mock_page.evaluate_handle.return_value
        with (
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6] as write,
            patches[7] as submit,
            patches[8],
            patches[9] as prepare,
            patches[10],
            patch.object(
                sender,
                "_wait_for_verified_submit",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch.object(
                sender, "_cleanup_owned_message", new_callable=AsyncMock
            ) as cleanup,
        ):
            result = await sender.send_message("testuser", "Hello!", confirm_send=True)

        assert result["status"] == "send_unavailable"
        assert result["retry_safe"] is True
        write.assert_awaited_once()
        prepare.assert_not_awaited()
        submit.assert_not_awaited()
        cleanup.assert_awaited_once_with("Hello!", owner)

    @pytest.mark.parametrize(
        ("submit_count", "submit_usable"),
        [(0, False), (2, False)],
        ids=["missing", "ambiguous"],
    )
    async def test_only_one_active_submit_path_can_send(
        self, mock_page, submit_count, submit_usable
    ):
        sender = _sender(mock_page)
        patches = self._patch_to_composer(
            sender,
            mock_page,
            states=[
                {"status": "valid"},
                {
                    "status": "valid",
                    "submitCount": submit_count,
                    "submitUsable": submit_usable,
                },
            ],
        )
        with (
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6] as write,
            patches[7] as submit,
            patches[8],
            patches[9],
            patches[10],
        ):
            result = await sender.send_message("testuser", "Hello!", confirm_send=True)

        assert result["status"] == "send_unavailable"
        assert result["retry_safe"] is True
        write.assert_not_awaited()
        submit.assert_not_awaited()
        mock_page.keyboard.press.assert_not_awaited()

    async def test_observer_is_prepared_after_typing_and_before_submission(
        self, mock_page
    ):
        """The mutation observer starts immediately before the only submit."""
        sender = _sender(mock_page)
        steps: list[str] = []
        patches = self._patch_to_composer(sender, mock_page)

        async def write(message, *, target, owner):
            assert message == "Hello!"
            assert target == self._target()
            assert owner is mock_page.evaluate_handle.return_value
            steps.append("write")
            return "written"

        async def prepare(message, *, target, owner):
            assert message == "Hello!"
            assert target == self._target()
            assert owner is mock_page.evaluate_handle.return_value
            steps.append("prepare")
            return "confirmation-token"

        async def submit(message, *, target, owner):
            assert message == "Hello!"
            assert target == self._target()
            assert owner is mock_page.evaluate_handle.return_value
            steps.append("submit")
            return "clicked"

        async def confirmed(message, *, target, owner, confirmation):
            assert message == "Hello!"
            assert target == self._target()
            assert owner is mock_page.evaluate_handle.return_value
            steps.append(f"confirm:{confirmation}")
            return True

        with (
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patch.object(
                sender,
                "_write_verified_message",
                new_callable=AsyncMock,
                side_effect=write,
            ),
            patch.object(
                sender,
                "_submit_verified_message",
                new_callable=AsyncMock,
                side_effect=submit,
            ),
            patches[8],
            patch.object(
                sender,
                "_prepare_message_confirmation",
                new_callable=AsyncMock,
                side_effect=prepare,
            ),
            patch.object(
                sender,
                "_message_send_confirmed",
                new_callable=AsyncMock,
                side_effect=confirmed,
            ),
        ):
            result = await sender.send_message("testuser", "Hello!", confirm_send=True)

        assert result["status"] == "sent"
        assert steps == [
            "write",
            "prepare",
            "submit",
            "confirm:confirmation-token",
        ]

    async def test_interrupted_submission_is_not_a_failure(self, mock_page):
        """A click round trip can fail after dispatching the local event."""
        sender = _sender(mock_page)
        patches = self._patch_to_composer(sender, mock_page)
        visible = AsyncMock()

        with (
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
            patches[7] as submit,
            patches[8],
            patches[9],
            patch.object(sender, "_message_send_confirmed", visible),
        ):
            submit.side_effect = PatchrightError("execution context was destroyed")
            result = await sender.send_message("testuser", "Hello!", confirm_send=True)

        assert result["status"] == "send_unconfirmed"
        assert result["sent"] is False
        assert result["retry_safe"] is False
        visible.assert_not_awaited()

    @pytest.mark.parametrize(
        "stage",
        ["dispatch", "confirmation", "owner-cleanup"],
    )
    async def test_cancellation_after_dispatch_is_logged(
        self, mock_page, caplog, stage
    ):
        """Cancellation in the destructive window leaves a warning behind."""
        sender = _sender(mock_page)
        patches = self._patch_to_composer(sender, mock_page)
        if stage == "owner-cleanup":
            mock_page.evaluate_handle.return_value.dispose = AsyncMock(
                side_effect=asyncio.CancelledError()
            )

        with (
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
            patches[7] as submit,
            patches[8],
            patches[9],
            patches[10] as confirmed,
            caplog.at_level(
                logging.WARNING, logger="linkedin_mcp_server.scraping.message_sender"
            ),
            pytest.raises(asyncio.CancelledError),
        ):
            if stage == "dispatch":
                submit.side_effect = asyncio.CancelledError()
            elif stage == "confirmation":
                confirmed.side_effect = asyncio.CancelledError()
            await sender.send_message("testuser", "Hello!", confirm_send=True)

        # Cancellation has to keep propagating, or the surrounding scope
        # never unwinds. The warning names the duplicate-delivery risk that
        # the discarded result can no longer report.
        warnings = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("retry may deliver the message twice" in w for w in warnings), (
            warnings
        )

    async def test_cancellation_while_writing_does_not_warn(self, mock_page, caplog):
        """Validated text cannot submit before the explicit submit path."""
        sender = _sender(mock_page)
        patches = self._patch_to_composer(sender, mock_page)

        with (
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6] as write,
            patches[7],
            patches[8],
            patches[9],
            patches[10],
            caplog.at_level(
                logging.WARNING, logger="linkedin_mcp_server.scraping.message_sender"
            ),
            pytest.raises(asyncio.CancelledError),
        ):
            write.side_effect = asyncio.CancelledError()
            await sender.send_message("testuser", "Hello there!", confirm_send=True)

        warnings = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
        assert not any("retry may deliver the message twice" in w for w in warnings)

    async def test_ordinary_error_after_dispatch_still_answers(self, mock_page):
        sender = _sender(mock_page)
        patches = self._patch_to_composer(sender, mock_page)

        with (
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
            patches[7],
            patches[8],
            patches[9],
            patches[10] as confirmed,
        ):
            confirmed.side_effect = RuntimeError("context destroyed")
            result = await sender.send_message("testuser", "Hello!", confirm_send=True)

        assert result["status"] == "send_unconfirmed"
        assert result["sent"] is False
        assert result["retry_safe"] is False

    async def test_owner_cleanup_runs_when_confirmation_cleanup_fails(self, mock_page):
        sender = _sender(mock_page)
        patches = self._patch_to_composer(sender, mock_page)
        owner = mock_page.evaluate_handle.return_value

        with (
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
            patches[7],
            patches[8],
            patches[9],
            patches[10],
            patch.object(
                sender,
                "_dispose_message_confirmation",
                new_callable=AsyncMock,
                side_effect=RuntimeError("cleanup failed"),
            ),
            patch.object(
                sender, "_dispose_message_owner", new_callable=AsyncMock
            ) as dispose_owner,
        ):
            result = await sender.send_message("testuser", "Hello!", confirm_send=True)

        assert result["status"] == "send_unconfirmed"
        assert result["retry_safe"] is False
        dispose_owner.assert_awaited_once_with(owner)

    async def test_an_error_before_anything_can_submit_is_raised(self, mock_page):
        """Without a newline nothing has submitted yet, so the error is the answer.

        The pair to the case above. Reporting `send_unconfirmed` here would
        claim a duplicate-delivery risk that cannot exist and take the real
        error away from a caller who can simply retry.
        """
        sender = _sender(mock_page)
        patches = self._patch_to_composer(sender, mock_page)

        with (
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6] as write,
            patches[7],
            patches[8],
            patches[9],
            patches[10],
            pytest.raises(RuntimeError, match="page closed"),
        ):
            write.side_effect = RuntimeError("page closed")
            await sender.send_message("testuser", "Single line", confirm_send=True)

    async def test_send_unconfirmed_when_click_adds_nothing(self, mock_page):
        """A clicked Send button that changes nothing is not a sent message."""
        sender = _sender(mock_page)
        patches = self._patch_to_composer(sender, mock_page)
        with (
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
            patches[7],
            patches[8],
            patch.object(
                sender,
                "_prepare_message_confirmation",
                new_callable=AsyncMock,
                return_value=1,
            ),
            patch.object(
                sender,
                "_message_send_confirmed",
                new_callable=AsyncMock,
                return_value=False,
            ) as visible,
        ):
            result = await sender.send_message("testuser", "Hello!", confirm_send=True)

        # The click happened, so nothing here proves the message did not go
        # out. Answering "not sent" would invite a retry that delivers twice,
        # which is what `retry_safe` says and `sent` cannot.
        assert result["status"] == "send_unconfirmed"
        assert result["sent"] is False
        assert result["retry_safe"] is False
        visible.assert_awaited_once_with(
            "Hello!",
            target=self._target(),
            owner=mock_page.evaluate_handle.return_value,
            confirmation=1,
        )


class TestResolveMessageComposeBox:
    async def test_requires_exactly_one_visible_editor(self, mock_page):
        sender = _sender(mock_page)
        locator = MagicMock(count=AsyncMock(return_value=2))
        locator.first = MagicMock()
        mock_page.locator.return_value = locator

        assert await sender._resolve_message_compose_box() is None

        mock_page.locator.assert_called_once_with(
            f"{message_sender_module._MESSAGING_COMPOSE_SELECTOR}:visible"
        )


class TestMessageConfirmation:
    """Tests for the owner-pinned message-list mutation contract."""

    @staticmethod
    def _arguments():
        target = TestSendMessage._target()
        owner = MagicMock()
        return target, owner

    async def test_owner_handle_uses_the_shared_recipient_inspection(self, mock_page):
        sender = _sender(mock_page)
        target, owner = self._arguments()
        owner.as_element.return_value = owner
        mock_page.evaluate_handle = AsyncMock(return_value=owner)

        expected_route = "https://www.linkedin.com/messaging/thread/ALICE/"

        assert (
            await sender._resolve_message_owner(target, expected_route=expected_route)
            is owner
        )

        mock_page.evaluate_handle.assert_awaited_once_with(
            _MESSAGE_COMPOSER_OWNER_JS,
            arg={
                "target": {
                    "profilePath": target.profile_path,
                    "profileUrn": target.profile_urn,
                },
                "expectedRoute": expected_route,
            },
        )

    async def test_invalid_owner_handle_is_released(self, mock_page):
        sender = _sender(mock_page)
        target, owner = self._arguments()
        owner.as_element.return_value = None
        owner.dispose = AsyncMock()
        mock_page.evaluate_handle = AsyncMock(return_value=owner)

        assert (
            await sender._resolve_message_owner(
                target,
                expected_route="https://www.linkedin.com/messaging/thread/ALICE/",
            )
            is None
        )
        owner.dispose.assert_awaited_once_with()

    async def test_owner_disposal_error_is_suppressed(self, mock_page):
        sender = _sender(mock_page)
        owner = MagicMock(dispose=AsyncMock(side_effect=RuntimeError("closed")))

        await sender._dispose_message_owner(owner)

        owner.dispose.assert_awaited_once_with()

    async def test_prepare_installs_observer_in_the_target_owner(self, mock_page):
        sender = _sender(mock_page)
        target, owner = self._arguments()
        mock_page.evaluate = AsyncMock(return_value="confirmation-token")

        assert (
            await sender._prepare_message_confirmation(
                "Hello!", target=target, owner=owner
            )
            == "confirmation-token"
        )
        mock_page.evaluate.assert_awaited_once_with(
            _MESSAGE_CONFIRMATION_PREPARE_JS,
            {
                "profilePath": target.profile_path,
                "profileUrn": target.profile_urn,
                "expected": "Hello!",
                "owner": owner,
            },
        )

    @pytest.mark.parametrize("result", [None, "", 0, {"token": "wrong"}])
    async def test_invalid_prepare_result_fails_closed(self, mock_page, result):
        sender = _sender(mock_page)
        target, owner = self._arguments()
        mock_page.evaluate = AsyncMock(return_value=result)

        assert (
            await sender._prepare_message_confirmation(
                "Hello!", target=target, owner=owner
            )
            is None
        )

    async def test_confirmation_waits_for_the_exact_token(self, mock_page):
        sender = _sender(mock_page)
        target, owner = self._arguments()
        mock_page.wait_for_function = AsyncMock(return_value=None)

        assert (
            await sender._message_send_confirmed(
                "Hello!",
                target=target,
                owner=owner,
                confirmation="confirmation-token",
            )
            is True
        )
        mock_page.wait_for_function.assert_awaited_once_with(
            _MESSAGE_CONFIRMATION_READY_JS,
            arg={
                "profilePath": target.profile_path,
                "profileUrn": target.profile_urn,
                "expected": "Hello!",
                "owner": owner,
                "token": "confirmation-token",
            },
        )

    @pytest.mark.parametrize(
        "error",
        [
            PlaywrightTimeoutError("timeout"),
            PatchrightError("execution context destroyed"),
        ],
        ids=["timeout", "context-destroyed"],
    )
    async def test_confirmation_errors_do_not_confirm(self, mock_page, error):
        sender = _sender(mock_page)
        target, owner = self._arguments()
        mock_page.wait_for_function = AsyncMock(side_effect=error)

        assert (
            await sender._message_send_confirmed(
                "Hello!",
                target=target,
                owner=owner,
                confirmation="confirmation-token",
            )
            is False
        )

    async def test_dispose_disconnects_the_owner_token(self, mock_page):
        sender = _sender(mock_page)
        _target, owner = self._arguments()
        mock_page.evaluate = AsyncMock()

        await sender._dispose_message_confirmation(owner, "confirmation-token")

        mock_page.evaluate.assert_awaited_once_with(
            _MESSAGE_CONFIRMATION_DISPOSE_JS,
            {"owner": owner, "token": "confirmation-token"},
        )
