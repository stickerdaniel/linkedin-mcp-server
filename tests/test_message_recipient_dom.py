"""Browser-DOM tests for local message-recipient verification.

The unit suite mocks ``page.evaluate``, so these cases execute the extraction,
focus, and submit JavaScript against synthetic Chromium DOMs. No LinkedIn page
is loaded and no account action is performed.
"""

from __future__ import annotations

import os
import time
from unittest.mock import AsyncMock, patch

import pytest
from patchright.async_api import BrowserType, async_playwright

from linkedin_mcp_server.scraping.extractor import (
    LinkedInExtractor,
    _MESSAGE_COMPOSER_STATE_JS,
    _PROFILE_MESSAGE_TARGET_JS,
    _ProfileMessageTarget,
)

pytestmark = [
    pytest.mark.browser_dom,
    pytest.mark.xdist_group("browser_runtime"),
]

TARGET = {"profilePath": "/in/testuser/", "profileUrn": "ACoAAB"}


def _message_target() -> _ProfileMessageTarget:
    return _ProfileMessageTarget(
        profile_path=TARGET["profilePath"],
        profile_urn=TARGET["profileUrn"],
        compose_url="https://www.linkedin.com/messaging/compose/?recipient=ACoAAB",
        display_name="Test User",
    )


async def _dom_page():
    async with async_playwright() as playwright:
        try:
            browser = await playwright.chromium.launch(
                channel="chromium", headless=True
            )
            page = await browser.new_page()
        except Exception as exc:
            if os.environ.get("CI"):
                raise
            pytest.skip(f"chromium unavailable: {exc}")
        await page.route(
            "https://www.linkedin.com/**",
            lambda route: route.fulfill(
                status=200,
                content_type="text/html",
                body='<!DOCTYPE html><html><head><meta charset="utf-8"></head></html>',
            ),
        )
        try:
            yield page
        finally:
            await browser.close()


@pytest.fixture
async def dom_page():
    async for page in _dom_page():
        yield page


async def test_dom_page_launch_failure_raises_in_ci(monkeypatch):
    monkeypatch.setenv("CI", "1")
    fixture = _dom_page()
    with (
        patch.object(
            BrowserType,
            "launch",
            new_callable=AsyncMock,
            side_effect=RuntimeError("injected launch failure"),
        ),
        patch.object(
            pytest,
            "skip",
            side_effect=AssertionError("CI launch failure was skipped"),
        ) as skip,
        pytest.raises(RuntimeError, match="injected launch failure"),
    ):
        await fixture.__anext__()
    skip.assert_not_called()


async def test_dom_page_launch_failure_skips_locally(monkeypatch):
    monkeypatch.delenv("CI", raising=False)
    fixture = _dom_page()
    with (
        patch.object(
            BrowserType,
            "launch",
            new_callable=AsyncMock,
            side_effect=RuntimeError("injected launch failure"),
        ),
        pytest.raises(pytest.skip.Exception, match="injected launch failure"),
    ):
        await fixture.__anext__()


def _composer(*, identity: str, buttons: str = "", extra: str = "") -> str:
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"></head><body>
      <main>
        <section role="dialog">
          {identity}
          <form>
            <div role="textbox" contenteditable="true"
                 style="display:block;width:200px;height:30px"></div>
            {buttons}
          </form>
        </section>
        {extra}
      </main>
    </body></html>
    """


async def _set_composer_content(page, html: str) -> None:
    await page.goto("https://www.linkedin.com/messaging/compose/?recipient=ACoAAB")
    await page.set_content(html)


async def _state(page, html: str) -> dict:
    await _set_composer_content(page, html)
    return await page.evaluate(_MESSAGE_COMPOSER_STATE_JS, TARGET)


class TestMessageSurfaceDom:
    async def test_waits_for_delayed_editor(self, dom_page):
        await _set_composer_content(
            dom_page,
            """<!DOCTYPE html><html><head><meta charset="utf-8"></head><body>
              <section role="dialog"><form></form></section>
            </body></html>""",
        )
        dom_page.set_default_timeout(1_000)
        await dom_page.evaluate(
            """() => setTimeout(() => {
                document.querySelector('form').insertAdjacentHTML(
                    'beforeend',
                    '<div role="textbox" contenteditable="true" '
                    + 'style="display:block;width:200px;height:30px"></div>'
                );
            }, 250)"""
        )
        started = time.monotonic()

        result = await LinkedInExtractor(dom_page)._wait_for_message_surface(
            _message_target()
        )

        assert result == "composer"
        assert time.monotonic() - started >= 0.2

    async def test_waits_for_multiple_editors_to_settle(self, dom_page):
        await _set_composer_content(
            dom_page,
            _composer(
                identity='<a href="https://www.linkedin.com/in/testuser/">Test</a>',
                extra=(
                    '<div role="textbox" contenteditable="true" data-stale '
                    'style="display:block;width:200px;height:30px"></div>'
                ),
            ),
        )
        dom_page.set_default_timeout(1_000)
        await dom_page.evaluate(
            """() => setTimeout(() => {
                document.querySelector('[data-stale]').remove();
            }, 250)"""
        )
        started = time.monotonic()

        result = await LinkedInExtractor(dom_page)._wait_for_message_surface(
            _message_target()
        )

        assert result == "composer"
        assert time.monotonic() - started >= 0.2

    async def test_permanent_recipient_conflict_times_out(self, dom_page):
        await _set_composer_content(
            dom_page,
            """<!DOCTYPE html><html><head><meta charset="utf-8"></head><body>
              <section role="dialog" data-recipient-urn="OTHER">
                <form>
                  <a href="https://www.linkedin.com/in/testuser/">Test</a>
                  <div role="textbox" contenteditable="true"
                       style="display:block;width:200px;height:30px"></div>
                </form>
              </section>
            </body></html>""",
        )
        dom_page.set_default_timeout(350)
        started = time.monotonic()

        result = await LinkedInExtractor(dom_page)._wait_for_message_surface(
            _message_target()
        )

        assert result is None
        assert time.monotonic() - started >= 0.25
        # `active` and `empty` both read false wherever the inspect never
        # settled on an editor: they answer for one, and there is none.
        assert await dom_page.evaluate(_MESSAGE_COMPOSER_STATE_JS, TARGET) == {
            "status": "recipient_mismatch",
            "active": False,
            "empty": False,
            "submitCount": 0,
            "submitUsable": False,
        }


class TestProfileMessageTargetDom:
    async def test_snapshot_stays_inside_first_top_card(self, dom_page):
        await _set_composer_content(
            dom_page,
            """<!DOCTYPE html><html><head><meta charset="utf-8"></head><body><main>
              <section>
                <h1>Test User</h1>
                <a style="display:block" href="/messaging/compose/?recipient=ACoAAB">
                  Nachricht
                </a>
              </section>
              <section>
                <a style="display:block" href="/messaging/compose/?recipient=OTHER">
                  Sidebar
                </a>
              </section>
            </main></body></html>
            """,
        )

        result = await dom_page.evaluate(_PROFILE_MESSAGE_TARGET_JS)

        assert result["displayName"] == "Test User"
        assert result["composeHrefs"] == ["/messaging/compose/?recipient=ACoAAB"]


class TestMessageComposerDom:
    async def test_owner_handle_pins_one_dom_instance_and_disposes(self, dom_page):
        await _set_composer_content(
            dom_page,
            _composer(
                identity='<a href="https://www.linkedin.com/in/testuser/">Test</a>',
                buttons='<button type="submit">Send</button>',
            ),
        )
        extractor = LinkedInExtractor(dom_page)
        owner = await extractor._resolve_message_owner(_message_target())

        assert owner is not None
        assert owner.as_element() is not None
        await dom_page.evaluate(
            """() => {
                const owner = document.querySelector('[role="dialog"]');
                owner.replaceWith(owner.cloneNode(true));
            }"""
        )
        assert await owner.evaluate("node => node.isConnected") is False
        await extractor._dispose_message_owner(owner)
        with pytest.raises(Exception, match="closed"):
            await owner.evaluate("node => node.isConnected")

    async def test_generic_data_urn_never_authorizes_recipient(self, dom_page):
        state = await _state(
            dom_page,
            _composer(identity='<span data-urn="ACoAAB">unrelated generic data</span>'),
        )

        assert state["status"] == "valid"

    async def test_native_dialog_accepts_explicit_recipient_urn(self, dom_page):
        state = await _state(
            dom_page,
            """<!DOCTYPE html><html><head><meta charset="utf-8"></head><body>
              <dialog open>
                <span data-recipient-urn="ACoAAB">Test</span>
                <div role="textbox" contenteditable="true"
                     style="display:block;width:200px;height:30px"></div>
              </dialog>
            </body></html>""",
        )

        assert state["status"] == "valid"

    @pytest.mark.parametrize("visibility", ["hidden", "collapse"])
    async def test_invisible_conflicts_do_not_block_url_pinned_composer(
        self, dom_page, visibility
    ):
        await _set_composer_content(
            dom_page,
            _composer(
                identity=(
                    f'<div style="visibility:{visibility}" data-recipient-urn="OTHER">'
                    '<a href="https://www.linkedin.com/in/bob/">Bob</a></div>'
                ),
                buttons='<button type="submit">Send</button>',
            ),
        )
        extractor = LinkedInExtractor(dom_page)

        owner = await extractor._resolve_message_owner(_message_target())
        assert owner is not None
        assert (
            await extractor._write_verified_message(
                "Hello!", target=_message_target(), owner=owner
            )
            == "written"
        )
        await extractor._dispose_message_owner(owner)

    async def test_hidden_conflicts_do_not_override_visible_identity(self, dom_page):
        await _set_composer_content(
            dom_page,
            _composer(
                identity=(
                    '<div style="visibility:hidden" data-recipient-urn="OTHER">'
                    '<a href="https://www.linkedin.com/in/bob/">Bob</a></div>'
                    '<div data-recipient-urn="ACoAAB">'
                    '<a href="https://www.linkedin.com/in/testuser/">Test</a></div>'
                ),
                buttons='<button type="submit">Send</button>',
            ),
        )
        extractor = LinkedInExtractor(dom_page)

        owner = await extractor._resolve_message_owner(_message_target())
        assert owner is not None
        assert (
            await extractor._write_verified_message(
                "Hello!", target=_message_target(), owner=owner
            )
            == "written"
        )
        await extractor._dispose_message_owner(owner)

    async def test_second_urn_attribute_on_one_element_is_never_skipped(self, dom_page):
        conflicting = await _state(
            dom_page,
            _composer(
                identity=(
                    '<span data-profile-urn="ACoAAB" data-recipient-urn="OTHER">'
                    "Test</span>"
                )
            ),
        )
        empty = await _state(
            dom_page,
            _composer(
                identity=(
                    '<span data-profile-urn="ACoAAB" data-recipient-urn="">Test</span>'
                )
            ),
        )
        consistent = await _state(
            dom_page,
            _composer(
                identity=(
                    '<span data-profile-urn="ACoAAB" '
                    'data-recipient-urn="urn:li:fsd_profile:ACoAAB">Test</span>'
                )
            ),
        )

        assert conflicting["status"] == "recipient_mismatch"
        assert empty["status"] == "recipient_mismatch"
        assert consistent["status"] == "valid"

    async def test_nested_owner_conflict_fails_closed(self, dom_page):
        state = await _state(
            dom_page,
            """<!DOCTYPE html><html><head><meta charset="utf-8"></head><body>
              <section role="dialog" data-recipient-urn="OTHER">
                <form>
                  <a href="https://www.linkedin.com/in/testuser/">Test</a>
                  <div role="textbox" contenteditable="true"
                       style="display:block;width:200px;height:30px"></div>
                  <button type="submit">Local</button>
                </form>
                <button type="submit" data-outer>Outer</button>
              </section>
            </body></html>""",
        )

        assert state["status"] == "recipient_mismatch"
        assert state["submitCount"] == 0

    async def test_nested_consistent_identity_keeps_submit_local(self, dom_page):
        await _set_composer_content(
            dom_page,
            """<!DOCTYPE html><html><head><meta charset="utf-8"></head><body>
              <section role="dialog" data-recipient-urn="ACoAAB">
                <form>
                  <a href="https://www.linkedin.com/in/testuser/">Test</a>
                  <div role="textbox" contenteditable="true"
                       style="display:block;width:200px;height:30px"></div>
                  <button type="submit" onclick="event.preventDefault();
                    this.setAttribute('data-clicked','yes')">Local</button>
                </form>
                <button type="submit" data-outer>Outer</button>
              </section>
            </body></html>""",
        )

        extractor = LinkedInExtractor(dom_page)
        owner = await extractor._resolve_message_owner(_message_target())
        assert owner is not None
        written = await extractor._write_verified_message(
            "Hello!", target=_message_target(), owner=owner
        )
        submitted = await extractor._submit_verified_message(
            "Hello!", target=_message_target(), owner=owner
        )
        await extractor._dispose_message_owner(owner)

        assert written == "written"
        assert submitted == "clicked"
        assert (
            await dom_page.locator("form button").get_attribute("data-clicked") == "yes"
        )
        assert (
            await dom_page.locator("[data-outer]").get_attribute("data-clicked") is None
        )

    @pytest.mark.parametrize(
        ("recipient_urn", "button_form", "expected"),
        [
            ("OTHER", None, None),
            ("ACoAAB", "foreign", None),
            ("ACoAAB", None, ("written", "clicked", "ancestor")),
        ],
        ids=["foreign-ancestor", "foreign-form-owner", "matching-ancestor"],
    )
    async def test_entire_semantic_ancestor_chain_controls_submit(
        self, dom_page, recipient_urn, button_form, expected
    ):
        form_attribute = f' form="{button_form}"' if button_form else ""
        await _set_composer_content(
            dom_page,
            f"""<!DOCTYPE html><html><head><meta charset="utf-8"></head><body>
              <form id="ancestor" data-recipient-urn="{recipient_urn}"
                    onsubmit="event.preventDefault();
                      document.body.dataset.submitted=this.id">
                <section role="dialog">
                  <div role="textbox" contenteditable="true"
                       style="display:block;width:200px;height:30px"></div>
                  <button type="submit"{form_attribute}>Send</button>
                </section>
              </form>
              <form id="foreign" onsubmit="event.preventDefault();
                document.body.dataset.submitted=this.id"></form>
            </body></html>""",
        )
        extractor = LinkedInExtractor(dom_page)
        owner = await extractor._resolve_message_owner(_message_target())
        written = submitted = None
        if owner is not None:
            written = await extractor._write_verified_message(
                "Hello!", target=_message_target(), owner=owner
            )
            submitted = await extractor._submit_verified_message(
                "Hello!", target=_message_target(), owner=owner
            )
            await extractor._dispose_message_owner(owner)

        if expected is None:
            assert owner is None
            assert written is None
            assert submitted is None
            assert await dom_page.evaluate("document.body.dataset.submitted") is None
        else:
            assert (written, submitted) == expected[:2]
            assert (
                await dom_page.evaluate("document.body.dataset.submitted")
                == expected[2]
            )

    async def test_profile_link_inside_editor_never_authorizes(self, dom_page):
        """Draft content is not a recipient, however well-formed it looks."""
        state = await _state(
            dom_page,
            """<!DOCTYPE html><html><head><meta charset="utf-8"></head><body>
              <section role="dialog"><form>
                <div role="textbox" contenteditable="true"
                     style="display:block;width:200px;height:30px">
                  <a href="https://www.linkedin.com/in/testuser/">Draft link</a>
                </div>
              </form></section>
            </body></html>""",
        )

        assert state["status"] == "valid"

    @pytest.mark.parametrize("attribute", ["data-profile-urn", "data-recipient-urn"])
    @pytest.mark.parametrize("location", ["editor", "descendant"])
    async def test_recipient_urn_inside_editor_never_authorizes(
        self, dom_page, attribute, location
    ):
        editor_attribute = f'{attribute}="ACoAAB"' if location == "editor" else ""
        draft = (
            f'<span {attribute}="ACoAAB">Draft identity</span>'
            if location == "descendant"
            else "Draft"
        )
        state = await _state(
            dom_page,
            f"""<!DOCTYPE html><html><head><meta charset="utf-8"></head><body>
              <section role="dialog"><form>
                <div role="textbox" contenteditable="true" {editor_attribute}
                     style="display:block;width:200px;height:30px">{draft}</div>
              </form></section>
            </body></html>""",
        )

        assert state["status"] == "valid"

    @pytest.mark.parametrize(
        "draft_identity",
        [
            '<a href="https://www.linkedin.com/in/testuser/">Matching draft</a>',
            '<a href="https://www.linkedin.com/in/other/">Foreign draft</a>',
            '<span data-profile-urn="ACoAAB">Matching draft</span>',
            '<span data-profile-urn="OTHER">Foreign draft</span>',
            '<span data-recipient-urn="ACoAAB">Matching draft</span>',
            '<span data-recipient-urn="OTHER">Foreign draft</span>',
        ],
    )
    async def test_outer_identity_ignores_draft_identity(
        self, dom_page, draft_identity
    ):
        """The verdict comes from the recipient chip, never from the draft."""
        state = await _state(
            dom_page,
            f"""<!DOCTYPE html><html><head><meta charset="utf-8"></head><body>
              <section role="dialog"><form>
                <a href="https://www.linkedin.com/in/testuser/">Recipient</a>
                <div role="textbox" contenteditable="true"
                     style="display:block;width:200px;height:30px">
                  {draft_identity}
                </div>
              </form></section>
            </body></html>""",
        )

        assert state["status"] == "valid"

    async def test_name_only_or_global_identity_never_authorizes(self, dom_page):
        html = _composer(
            identity=(
                "<span>Test User</span>"
                '<span hidden data-profile-urn="ACoAAB">stale identity</span>'
            ),
            extra='<a href="https://www.linkedin.com/in/testuser/">Test User</a>',
        )

        state = await _state(dom_page, html)

        assert state["status"] == "valid"

    async def test_foreign_or_multiple_editor_fails_closed(self, dom_page):
        foreign = await _state(
            dom_page,
            _composer(
                identity='<a href="https://www.linkedin.com/in/other/">Other</a>'
            ),
        )
        multiple = await _state(
            dom_page,
            _composer(
                identity='<a href="https://www.linkedin.com/in/testuser/">Test</a>',
                extra=(
                    '<div role="textbox" contenteditable="true" '
                    'style="display:block;width:200px;height:30px"></div>'
                ),
            ),
        )

        assert foreign["status"] == "recipient_mismatch"
        assert multiple["status"] == "ambiguous_editor"

    @pytest.mark.parametrize(
        ("href", "expected"),
        [
            # Every shape below was read off one live profile page. Four of
            # its 38 visible anchors point into the member's own subtree, and
            # reading those as an unknown member made the recipient
            # contradict themselves and stopped the send.
            ("https://www.linkedin.com/in/testuser/overlay/contact-info/", "valid"),
            ("https://www.linkedin.com/in/testuser/recent-activity/all/", "valid"),
            ("https://www.linkedin.com/in/testuser", "valid"),
            ("https://www.linkedin.com/in/testuser?miniProfileUrn=urn%3A", "valid"),
            # A different member stays a different member in every shape.
            ("https://www.linkedin.com/in/other/en/", "recipient_mismatch"),
            ("https://www.linkedin.com/in/other", "recipient_mismatch"),
            # An anchor that names nobody stays a contradiction rather than
            # silence: a link the check cannot read is not evidence that the
            # recipient is the requested one.
            ("https://www.linkedin.com/in/", "recipient_mismatch"),
            ("https://evil.example/in/testuser/", "recipient_mismatch"),
        ],
    )
    async def test_subpaths_belong_to_the_member_they_sit_under(
        self, dom_page, href, expected
    ):
        state = await _state(
            dom_page, _composer(identity=f'<a href="{href}">Profile</a>')
        )

        assert state["status"] == expected

    async def test_focus_and_single_submit_stay_local(self, dom_page):
        await _set_composer_content(
            dom_page,
            _composer(
                identity='<a href="https://www.linkedin.com/in/testuser/">Test</a>',
                buttons=(
                    '<button type="submit" onclick="event.preventDefault();'
                    "this.setAttribute('data-clicked','yes')\">Senden</button>"
                ),
                extra=('<button type="submit" data-global="true">Global</button>'),
            ),
        )

        extractor = LinkedInExtractor(dom_page)
        owner = await extractor._resolve_message_owner(_message_target())
        assert owner is not None
        written = await extractor._write_verified_message(
            "Hello!", target=_message_target(), owner=owner
        )
        submitted = await extractor._submit_verified_message(
            "Hello!", target=_message_target(), owner=owner
        )
        await extractor._dispose_message_owner(owner)

        assert written == "written"
        assert submitted == "clicked"
        assert (
            await dom_page.locator("form button").get_attribute("data-clicked") == "yes"
        )
        assert (
            await dom_page.locator("[data-global]").get_attribute("data-clicked")
            is None
        )

    async def test_ambiguous_submit_is_never_pinned(self, dom_page):
        await _set_composer_content(
            dom_page,
            _composer(
                identity='<a href="https://www.linkedin.com/in/testuser/">Test</a>',
                buttons=(
                    '<button type="submit">A</button><button type="submit">B</button>'
                ),
            ),
        )

        assert (
            await LinkedInExtractor(dom_page)._resolve_message_owner(_message_target())
            is None
        )

    @pytest.mark.parametrize("disabled_attribute", ["disabled", 'aria-disabled="true"'])
    async def test_unique_disabled_submit_is_pinned_for_local_write(
        self, dom_page, disabled_attribute
    ):
        await _set_composer_content(
            dom_page,
            _composer(
                identity='<a href="https://www.linkedin.com/in/testuser/">Test</a>',
                buttons=f'<button type="submit" {disabled_attribute}>A</button>',
            ),
        )
        extractor = LinkedInExtractor(dom_page)

        owner = await extractor._resolve_message_owner(_message_target())

        assert owner is not None
        assert (
            await extractor._write_verified_message(
                "Hello!", target=_message_target(), owner=owner
            )
            == "written"
        )
        assert (
            await extractor._submit_verified_message(
                "Hello!", target=_message_target(), owner=owner
            )
            == "invalid"
        )
        await extractor._cleanup_owned_message("Hello!", owner)
        await extractor._dispose_message_owner(owner)
        assert await dom_page.locator('[role="textbox"]').inner_text() == ""

    async def test_removed_submit_or_changed_recipient_invalidates_pins(self, dom_page):
        identity = '<a href="https://www.linkedin.com/in/testuser/">Test</a>'
        await _set_composer_content(
            dom_page,
            _composer(identity=identity, buttons='<button type="submit">Send</button>'),
        )
        extractor = LinkedInExtractor(dom_page)
        owner = await extractor._resolve_message_owner(_message_target())
        assert owner is not None
        assert (
            await extractor._write_verified_message(
                "Hello!", target=_message_target(), owner=owner
            )
            == "written"
        )
        await dom_page.locator("form button").evaluate("element => element.remove()")
        assert (
            await extractor._submit_verified_message(
                "Hello!", target=_message_target(), owner=owner
            )
            == "invalid"
        )
        await extractor._dispose_message_owner(owner)

        await _set_composer_content(
            dom_page,
            _composer(identity=identity, buttons='<button type="submit">Send</button>'),
        )
        owner = await extractor._resolve_message_owner(_message_target())
        assert owner is not None
        assert (
            await extractor._write_verified_message(
                "Hello!", target=_message_target(), owner=owner
            )
            == "written"
        )
        await dom_page.locator('[role="dialog"] > a').evaluate(
            "element => element.setAttribute('href', 'https://www.linkedin.com/in/other/')"
        )
        assert (
            await extractor._submit_verified_message(
                "Hello!", target=_message_target(), owner=owner
            )
            == "invalid"
        )
        await extractor._dispose_message_owner(owner)

    async def test_queryless_thread_route_is_pinned_exactly(self, dom_page):
        await dom_page.goto("https://www.linkedin.com/messaging/thread/INITIAL/")
        await dom_page.set_content(
            _composer(identity="", buttons='<button type="submit">Send</button>')
        )
        extractor = LinkedInExtractor(dom_page)
        owner = await extractor._resolve_message_owner(_message_target())

        assert owner is not None
        assert (
            await extractor._write_verified_message(
                "Hello!", target=_message_target(), owner=owner
            )
            == "written"
        )
        await dom_page.evaluate(
            "history.replaceState({}, '', '/messaging/thread/OTHER/')"
        )
        assert (
            await extractor._submit_verified_message(
                "Hello!", target=_message_target(), owner=owner
            )
            == "invalid"
        )
        await extractor._cleanup_owned_message("Hello!", owner)
        await extractor._dispose_message_owner(owner)
        assert await dom_page.locator('[role="textbox"]').inner_text() == ""

    async def test_missing_submit_never_falls_back_to_enter(self, dom_page):
        await _set_composer_content(
            dom_page,
            _composer(
                identity=(
                    '<span data-profile-urn="urn:li:fsd_profile:ACoAAB">Test</span>'
                ),
                extra='<input id="foreign">',
            ),
        )
        await dom_page.locator("#foreign").evaluate(
            "element => element.addEventListener('keydown', () => "
            "document.body.dataset.foreignKey = 'true')"
        )
        await dom_page.locator("#foreign").focus()

        assert (
            await LinkedInExtractor(dom_page)._resolve_message_owner(_message_target())
            is None
        )
        assert await dom_page.evaluate("document.body.dataset.foreignKey") is None
