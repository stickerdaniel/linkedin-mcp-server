"""Every read whose result is taken as LinkedIn content refuses another site.

Each case drives one reader's own script on a page that is a portal's by the
time it is read, with whatever plausible answer the script would give. A read
that skips ``PageSession.read_document`` returns that answer and fails here;
``tests/test_off_linkedin_landing_dom.py`` runs the wrapper itself in a real
browser.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from linkedin_mcp_server.core.exceptions import OffLinkedInLandingError
from linkedin_mcp_server.linkedin.capture import SectionCapture
from linkedin_mcp_server.linkedin.connection_actions import ConnectionActions
from linkedin_mcp_server.linkedin.content import PageContentReader
from linkedin_mcp_server.linkedin.conversations import ConversationReader
from linkedin_mcp_server.linkedin.job_pages import JobPageReader
from linkedin_mcp_server.linkedin.navigation import PageNavigator
from linkedin_mcp_server.linkedin.person import PersonReader
from linkedin_mcp_server.linkedin.profile_page import ProfilePageReader
from linkedin_mcp_server.linkedin.session import PageSession
from linkedin_mcp_server.linkedin.text import JOB_APPLY_EN_US

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
