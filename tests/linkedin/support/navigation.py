"""Page doubles shared by navigation and workflow tests."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture
def mock_page():
    """Create a mock Patchright page."""
    page = MagicMock()
    page.goto = AsyncMock()
    page.title = AsyncMock(return_value="LinkedIn")
    page.wait_for_selector = AsyncMock()
    page.wait_for_function = AsyncMock()
    page.url = "https://www.linkedin.com/in/testuser/"
    page.locator = MagicMock()
    # Default: no modals, no CAPTCHA
    mock_locator = MagicMock()
    mock_locator.count = AsyncMock(return_value=0)
    mock_locator.is_visible = AsyncMock(return_value=False)
    mock_locator.first = mock_locator
    mock_locator.inner_text = AsyncMock(return_value="normal page content")
    mock_locator.filter = MagicMock(return_value=mock_locator)
    page.locator.return_value = mock_locator
    # A real `Frame` carries the address it navigated to, and production
    # reads it off the `framenavigated` argument rather than off the page.
    # A bare object answers every attribute with nothing, which left hop
    # recording looking correct here however broken it was.
    page.main_frame = SimpleNamespace(url=page.url)
    page.wait_for_load_state = AsyncMock()
    # Real listeners, so that a double can navigate the way the browser does.
    # A reload leaves `page.url` untouched, so the event is the only thing that
    # says the document was replaced, and a double that only assigns the URL
    # cannot express one.
    listeners: dict[str, list] = {}
    page.on = MagicMock(
        side_effect=lambda event, cb: listeners.setdefault(event, []).append(cb)
    )
    page.remove_listener = MagicMock(
        side_effect=lambda event, cb: (
            listeners.get(event, []).remove(cb)
            if cb in listeners.get(event, [])
            else None
        )
    )
    page.listeners = listeners
    # The document's own identity, which `performance.timeOrigin` reports and
    # `navigate()` moves. A double answering every script with one object
    # claims a document that is never replaced, and the code under test reads
    # that as a page rewriting its own address.
    page.time_origin = 1_000.0
    page.evaluate = with_document_identity(
        page,
        AsyncMock(
            return_value={
                "source": "root",
                "text": "Sample page text",
                "references": [],
            }
        ),
    )
    return page


def with_document_identity(page, evaluate):
    """Answer the document-identity read from `page.time_origin`.

    Every other script keeps going to `evaluate`. A test that replaces
    `page.evaluate` outright loses this and reads no identity at all, which
    leaves the production code where it was before there was one: it settles
    on the event alone.
    """

    async def dispatch(script, *args, **kwargs):
        if "timeOrigin" in script:
            return page.time_origin
        return await evaluate(script, *args, **kwargs)

    return AsyncMock(side_effect=dispatch)


def navigate(page, url: str | None = None, same_document: bool = False) -> None:
    """Move a mock page the way a navigation does: the address and the event.

    `url` is omitted for a reload, which replaces the document and leaves the
    address exactly as it was.

    `same_document` is a `pushState`, a `replaceState` or a hash change. Each
    raises `framenavigated` on the main frame exactly as a replacement does
    (measured), and LinkedIn appends `currentJobId` to a search URL that way
    on every healthy page. What separates them is the document surviving.
    """
    if url is not None:
        page.url = url
    if not same_document:
        page.time_origin += 1.0
    # The frame carries the address too, and production reads the hop off the
    # frame rather than off the page. Leaving it behind is what let the hop
    # recording look correct here however broken it was.
    page.main_frame.url = page.url
    for callback in list(page.listeners.get("framenavigated", [])):
        callback(page.main_frame)
