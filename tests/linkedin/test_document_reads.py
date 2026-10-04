"""Every read and action on a LinkedIn page refuses another site.

Each case drives one reader's own script on a page that is a portal's by the
time it is read, with whatever plausible answer the script would give. A read
that skips ``PageSession.run_on_linkedin`` returns that answer and fails here;
``tests/test_off_linkedin_landing_dom.py`` runs the wrapper itself in a real
browser.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from linkedin_mcp_server.core.exceptions import OffLinkedInLandingError
from linkedin_mcp_server.core.utils import handle_modal_close
from linkedin_mcp_server.linkedin import session as session_module
from linkedin_mcp_server.linkedin.capture import (
    CaptureMode,
    CapturePlan,
    SectionCapture,
)
from linkedin_mcp_server.linkedin.connection_actions import ConnectionActions
from linkedin_mcp_server.linkedin.content import PageContentReader
from linkedin_mcp_server.linkedin.conversations import ConversationReader
from linkedin_mcp_server.linkedin.job_pages import JobPageReader
from linkedin_mcp_server.linkedin.navigation import PageNavigator
from linkedin_mcp_server.linkedin.person import PersonReader
from linkedin_mcp_server.linkedin.profile_page import ProfilePageReader
from linkedin_mcp_server.linkedin.session import PageSession
from linkedin_mcp_server.linkedin.text import JOB_APPLY_EN_US

from .support.navigation import held_in

PORTAL_URL = "https://portal.invalid/interstitial"

Read = Callable[[Any], Awaitable[Any]]


def _session(page: Any) -> PageSession:
    return PageSession(page)


def _content(page: Any) -> PageContentReader:
    return PageContentReader(_session(page))


def _jobs(page: Any) -> JobPageReader:
    session = _session(page)
    return JobPageReader(session, PageNavigator(session), PageContentReader(session))


def _profile_page(page: Any) -> ProfilePageReader:
    return ProfilePageReader(_session(page), AsyncMock())


def _person(page: Any) -> PersonReader:
    session = _session(page)
    navigator = PageNavigator(session)
    return PersonReader(
        session,
        navigator,
        SectionCapture(session, navigator, PageContentReader(session)),
        ProfilePageReader(session, AsyncMock()),
    )


def _conversations(page: Any) -> ConversationReader:
    session = _session(page)
    return ConversationReader(
        session,
        PageNavigator(session),
        PageContentReader(session),
        ProfilePageReader(session, AsyncMock()),
    )


def _connections(page: Any) -> ConnectionActions:
    session = _session(page)
    return ConnectionActions(session, PageNavigator(session), AsyncMock())


async def _sidebar(page: Any) -> Any:
    with patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock):
        return await _person(page).get_sidebar_profiles("testuser")


async def _expanded_sidebar(page: Any) -> Any:
    """The first read is LinkedIn's; the Show all page is a portal's."""
    page.url = "https://www.linkedin.com/in/testuser/"
    answers = iter(
        [
            {"sections": {}, "showAllUrls": {"more": "/in/testuser/more/"}},
            ["/in/someone/"],
        ]
    )
    page.evaluate = AsyncMock(side_effect=lambda *_a, **_k: next(answers))

    async def land_on_the_portal(url: str) -> None:
        if "more" in url:
            page.url = PORTAL_URL

    with patch.object(
        PageNavigator, "_navigate_to_page", side_effect=land_on_the_portal
    ):
        return await _person(page).get_sidebar_profiles("testuser")


async def _apply_link(page: Any) -> Any:
    with patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock):
        return await _jobs(page).read_apply_link(
            "https://www.linkedin.com/jobs/view/123/", "123", JOB_APPLY_EN_US
        )


async def _upsell(page: Any) -> Any:
    link = MagicMock()
    link.wait_for = AsyncMock()
    link.inner_text = AsyncMock(return_value="The portal's own link text")
    link.first = link
    page.locator.return_value = link
    return await _connections(page)._get_premium_upsell_message()


CASES: list[tuple[str, Read, Any]] = [
    ("page text", lambda p: _content(p).get_page_text(), "Portal text"),
    (
        "root content",
        lambda p: _content(p)._extract_root_content(["main"]),
        {"source": "root", "text": "Portal text", "references": []},
    ),
    (
        "apply signals",
        _apply_link,
        {"applied": False, "closed": False, "easy_apply": True, "external_link": None},
    ),
    (
        "job ids",
        lambda p: _jobs(p)._extract_job_ids(),
        {"ids": ["123"], "scoped": False},
    ),
    (
        "promoted job ids",
        lambda p: _jobs(p)._extract_promoted_job_ids("Promoted"),
        ["123"],
    ),
    ("search page count", lambda p: _jobs(p)._get_total_search_pages(), "1 of 9"),
    ("saved page count", lambda p: _jobs(p)._get_total_list_pages(), 9),
    ("sidebar profiles", _sidebar, {"sections": {}, "showAllUrls": {}}),
    ("expanded sidebar profiles", _expanded_sidebar, None),
    (
        "profile display name",
        lambda p: _profile_page(p)._read_profile_display_name(),
        "Portal User",
    ),
    (
        "conversation thread refs",
        lambda p: _conversations(p)._extract_conversation_thread_refs(5, "inbox"),
        {"refs": [], "rows": 0},
    ),
    (
        "action signals",
        lambda p: _connections(p)._read_action_signals("testuser"),
        {"hasInvite": True},
    ),
    ("premium upsell text", _upsell, "Portal dialog text"),
]


@pytest.mark.parametrize(
    ("read", "answer"),
    [(read, answer) for _name, read, answer in CASES],
    ids=[name for name, _read, _answer in CASES],
)
async def test_a_read_on_another_sites_page_is_refused(
    mock_page, read: Read, answer: Any
):
    mock_page.url = PORTAL_URL
    if answer is not None:
        mock_page.evaluate = AsyncMock(return_value=answer)

    with pytest.raises(OffLinkedInLandingError, match="https://portal.invalid"):
        await read(mock_page)


# Actions. A page script that clicks runs through `run_on_linkedin`, which
# refuses before the script runs; here the double cannot run a script, so the
# driver's address stands in and a bypass returns the click's answer instead
# of raising. A locator action resolves one handle first and asks that handle
# for its own document, so those doubles hold their element on the portal
# while the driver still reports LinkedIn: the handle, not the driver, decides.

LINKEDIN_URL = "https://www.linkedin.com/in/testuser/"


def _portal_element() -> MagicMock:
    element = held_in(MagicMock(), PORTAL_URL)
    for action in ("click", "fill", "press", "scroll_into_view_if_needed"):
        setattr(element, action, AsyncMock())
    element.count = AsyncMock(return_value=3)
    element.is_visible = AsyncMock(return_value=True)
    element.wait_for = AsyncMock()
    element.inner_text = AsyncMock(return_value="FOREIGN PORTAL MESSAGE")
    element.first = element
    element.filter = MagicMock(return_value=element)
    element.locator = MagicMock(return_value=element)
    element.nth = MagicMock(return_value=element)
    return element


async def _show_more(page: Any) -> Any:
    # The driver still says LinkedIn, so the content read after the loop would
    # succeed: only the handle's refusal, carried out of the loop, can fail it.
    page.url = LINKEDIN_URL
    session = _session(page)
    capture = SectionCapture(
        session, PageNavigator(session), PageContentReader(session)
    )
    with (
        patch.object(session_module, "scroll_to_bottom", new_callable=AsyncMock),
        patch.object(session_module, "detect_rate_limit", new_callable=AsyncMock),
        patch.object(session_module, "handle_modal_close", new_callable=AsyncMock),
    ):
        return await capture._extract_loaded_section(
            "https://www.linkedin.com/in/testuser/details/experience/",
            "experience",
            CapturePlan(CaptureMode.DETAILS),
        )


async def _keyboard_fallback(page: Any) -> Any:
    actions = _connections(page)
    with (
        patch.object(actions, "_dialog_is_open", AsyncMock(return_value=True)),
        patch.object(actions, "_fill_dialog_textarea", AsyncMock(return_value=True)),
        patch.object(
            actions, "_click_dialog_primary_button", AsyncMock(return_value=False)
        ),
    ):
        return await actions._submit_invite_dialog(None)


async def _add_note(page: Any) -> Any:
    actions = _connections(page)
    page.element.count = AsyncMock(side_effect=[0, 2])
    with patch.object(actions, "_dialog_is_open", AsyncMock(return_value=True)):
        return await actions._submit_invite_dialog("Hello")


async def _quota_probe(page: Any) -> Any:
    actions = _connections(page)
    page.element.count = AsyncMock(side_effect=[0, 3])
    with (
        patch.object(actions, "_dialog_is_open", AsyncMock(return_value=True)),
        patch.object(
            actions, "_get_premium_upsell_message", AsyncMock(return_value=None)
        ),
    ):
        return await actions._probe_invite_note_limit()


ACTIONS: list[tuple[str, Read]] = [
    ("open the More menu", lambda p: _connections(p)._open_more_menu()),
    ("accept an incoming request", lambda p: _connections(p)._click_incoming_accept()),
    (
        "scroll the conversation list",
        lambda p: _conversations(p)._scroll_main_scrollable_region(
            position="bottom", attempts=1
        ),
    ),
    ("click a button by its text", lambda p: _content(p).click_button_by_text("Go")),
    ("click a details Show more", _show_more),
    (
        "click the dialog's primary button",
        lambda p: _connections(p)._click_dialog_primary_button(),
    ),
    ("fill the invite note", lambda p: _connections(p)._fill_dialog_textarea("Hi")),
    ("open the note editor", _add_note),
    ("press Enter on the primary button", _keyboard_fallback),
    ("open the note editor for the quota probe", _quota_probe),
    ("close a modal", lambda p: handle_modal_close(p)),
]


@pytest.mark.parametrize(
    "act", [act for _name, act in ACTIONS], ids=[name for name, _act in ACTIONS]
)
async def test_an_action_on_another_sites_page_is_refused_unperformed(
    mock_page, act: Read
):
    element = _portal_element()
    mock_page.element = element
    mock_page.locator = MagicMock(return_value=element)
    mock_page.url = PORTAL_URL
    mock_page.evaluate = AsyncMock(return_value=True)

    with pytest.raises(OffLinkedInLandingError, match="https://portal.invalid"):
        await act(mock_page)

    for action in ("click", "fill", "press"):
        getattr(element, action).assert_not_awaited()


async def test_a_handle_on_a_portal_is_refused_while_the_driver_says_linkedin(
    mock_page,
):
    """The driver's address lags the redirect; the element's own document does not."""
    element = _portal_element()
    mock_page.locator = MagicMock(return_value=element)
    mock_page.url = LINKEDIN_URL

    with pytest.raises(OffLinkedInLandingError, match="https://portal.invalid"):
        await _connections(mock_page)._click_dialog_primary_button()

    element.click.assert_not_awaited()


class TestTheUpsellFallbackReadsOnlyLinkedIn:
    """The snapshot can fail because the page left; the fallback must not follow."""

    async def test_an_interrupted_snapshot_on_a_portal_is_refused(self, mock_page):
        mock_page.url = PORTAL_URL
        mock_page.locator = MagicMock(return_value=_portal_element())
        mock_page.evaluate = AsyncMock(
            side_effect=Exception(
                "Execution context was destroyed, most likely because of a navigation"
            )
        )

        with pytest.raises(OffLinkedInLandingError, match="https://portal.invalid"):
            await _connections(mock_page)._get_premium_upsell_message()

    async def test_the_link_is_judged_by_its_own_document(self, mock_page):
        """The driver still says LinkedIn; the link is on the portal."""
        mock_page.url = LINKEDIN_URL
        mock_page.locator = MagicMock(return_value=_portal_element())
        mock_page.evaluate = AsyncMock(side_effect=Exception("context destroyed"))

        with pytest.raises(OffLinkedInLandingError, match="https://portal.invalid"):
            await _connections(mock_page)._get_premium_upsell_message()

    async def test_no_link_left_on_a_portal_is_not_a_detected_modal(self, mock_page):
        link = _portal_element()
        link.element_handle = AsyncMock(side_effect=Exception("detached"))
        mock_page.url = PORTAL_URL
        mock_page.locator = MagicMock(return_value=link)
        mock_page.evaluate = AsyncMock(side_effect=Exception("context destroyed"))

        with pytest.raises(OffLinkedInLandingError, match="https://portal.invalid"):
            await _connections(mock_page)._get_premium_upsell_message()

    async def test_linkedin_s_link_text_is_still_the_fallback(self, mock_page):
        link = held_in(MagicMock(), LINKEDIN_URL)
        link.first = link
        link.wait_for = AsyncMock()
        link.inner_text = AsyncMock(return_value="Upgrade to send more notes")
        mock_page.url = LINKEDIN_URL
        mock_page.locator = MagicMock(return_value=link)
        mock_page.evaluate = AsyncMock(side_effect=Exception("context destroyed"))

        message = await _connections(mock_page)._get_premium_upsell_message()

        assert message == "Upgrade to send more notes"
