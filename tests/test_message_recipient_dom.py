"""Browser-DOM tests for local message-recipient verification.

The unit suite mocks ``page.evaluate``, so these cases execute the extraction,
focus, and submit JavaScript against synthetic Chromium DOMs. No LinkedIn page
is loaded and no account action is performed.
"""

from __future__ import annotations

import time

import pytest
from patchright.async_api import async_playwright

from linkedin_mcp_server.scraping.extractor import (
    LinkedInExtractor,
    _MESSAGE_COMPOSER_FOCUS_JS,
    _MESSAGE_COMPOSER_STATE_JS,
    _MESSAGE_COMPOSER_SUBMIT_JS,
    _PROFILE_MESSAGE_TARGET_JS,
    _ProfileMessageTarget,
)

pytestmark = [
    pytest.mark.browser_dom,
    pytest.mark.xdist_group("browser_runtime"),
]

TARGET = {"profilePath": "/in/testuser/", "profileUrn": "ACoAAB"}
ENTER_TARGET = {**TARGET, "allowEnter": True}


def _message_target() -> _ProfileMessageTarget:
    return _ProfileMessageTarget(
        profile_path=TARGET["profilePath"],
        profile_urn=TARGET["profileUrn"],
        compose_url="https://www.linkedin.com/messaging/compose/?recipient=ACoAAB",
        display_name="Test User",
    )


@pytest.fixture
async def dom_page():
    async with async_playwright() as playwright:
        try:
            browser = await playwright.chromium.launch(
                channel="chromium", headless=True
            )
            page = await browser.new_page()
        except Exception as exc:
            pytest.skip(f"chromium unavailable: {exc}")
        try:
            yield page
        finally:
            await browser.close()


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


async def _state(page, html: str) -> dict:
    await page.set_content(html)
    return await page.evaluate(_MESSAGE_COMPOSER_STATE_JS, TARGET)


class TestMessageSurfaceDom:
    async def test_waits_for_delayed_recipient_identity(self, dom_page):
        await dom_page.set_content(
            """<!DOCTYPE html><html><head><meta charset="utf-8"></head><body>
              <section role="dialog">
                <form>
                  <div role="textbox" contenteditable="true"
                       style="display:block;width:200px;height:30px"></div>
                </form>
              </section>
            </body></html>"""
        )
        dom_page.set_default_timeout(1_000)
        await dom_page.evaluate(
            """() => setTimeout(() => {
                document.querySelector('form').insertAdjacentHTML(
                    'afterbegin',
                    '<a href="https://www.linkedin.com/in/testuser/">Test</a>'
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
        await dom_page.set_content(
            _composer(
                identity='<a href="https://www.linkedin.com/in/testuser/">Test</a>',
                extra=(
                    '<div role="textbox" contenteditable="true" data-stale '
                    'style="display:block;width:200px;height:30px"></div>'
                ),
            )
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
        await dom_page.set_content(
            """<!DOCTYPE html><html><head><meta charset="utf-8"></head><body>
              <section role="dialog" data-recipient-urn="OTHER">
                <form>
                  <a href="https://www.linkedin.com/in/testuser/">Test</a>
                  <div role="textbox" contenteditable="true"
                       style="display:block;width:200px;height:30px"></div>
                </form>
              </section>
            </body></html>"""
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
        }


class TestProfileMessageTargetDom:
    async def test_snapshot_stays_inside_first_top_card(self, dom_page):
        await dom_page.set_content(
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
            """
        )

        result = await dom_page.evaluate(_PROFILE_MESSAGE_TARGET_JS)

        assert result["displayName"] == "Test User"
        assert result["composeHrefs"] == ["/messaging/compose/?recipient=ACoAAB"]


class TestMessageComposerDom:
    async def test_generic_data_urn_never_authorizes_recipient(self, dom_page):
        state = await _state(
            dom_page,
            _composer(identity='<span data-urn="ACoAAB">unrelated generic data</span>'),
        )

        assert state["status"] == "missing_recipient"

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
        await dom_page.set_content(
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
            </body></html>"""
        )

        focused = await dom_page.evaluate(_MESSAGE_COMPOSER_FOCUS_JS, TARGET)
        submitted = await dom_page.evaluate(_MESSAGE_COMPOSER_SUBMIT_JS, TARGET)

        assert focused is True
        assert submitted == "clicked"
        assert (
            await dom_page.locator("form button").get_attribute("data-clicked") == "yes"
        )
        assert (
            await dom_page.locator("[data-outer]").get_attribute("data-clicked") is None
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

        assert state["status"] == "missing_recipient"

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

        assert state["status"] == "missing_recipient"

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

        assert state["status"] == "missing_recipient"

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
        await dom_page.set_content(
            _composer(
                identity='<a href="https://www.linkedin.com/in/testuser/">Test</a>',
                buttons=(
                    '<button type="submit" onclick="event.preventDefault();'
                    "this.setAttribute('data-clicked','yes')\">Senden</button>"
                ),
                extra=('<button type="submit" data-global="true">Global</button>'),
            )
        )

        focused = await dom_page.evaluate(_MESSAGE_COMPOSER_FOCUS_JS, TARGET)
        submitted = await dom_page.evaluate(_MESSAGE_COMPOSER_SUBMIT_JS, TARGET)

        assert focused is True
        assert submitted == "clicked"
        assert (
            await dom_page.locator("form button").get_attribute("data-clicked") == "yes"
        )
        assert (
            await dom_page.locator("[data-global]").get_attribute("data-clicked")
            is None
        )

    async def test_ambiguous_disabled_and_changed_recipient_never_submit(
        self, dom_page
    ):
        identity = '<a href="https://www.linkedin.com/in/testuser/">Test</a>'
        await dom_page.set_content(
            _composer(
                identity=identity,
                buttons='<button type="submit">A</button><button type="submit">B</button>',
            )
        )
        assert await dom_page.evaluate(_MESSAGE_COMPOSER_FOCUS_JS, TARGET) is True
        assert await dom_page.evaluate(_MESSAGE_COMPOSER_SUBMIT_JS, TARGET) == "invalid"

        await dom_page.set_content(
            _composer(
                identity=identity, buttons='<button type="submit" disabled>A</button>'
            )
        )
        assert await dom_page.evaluate(_MESSAGE_COMPOSER_FOCUS_JS, TARGET) is True
        assert await dom_page.evaluate(_MESSAGE_COMPOSER_SUBMIT_JS, TARGET) == "invalid"

        await dom_page.set_content(
            _composer(
                identity=identity,
                buttons='<button type="submit" aria-disabled="true">A</button>',
            )
        )
        assert await dom_page.evaluate(_MESSAGE_COMPOSER_FOCUS_JS, TARGET) is True
        assert await dom_page.evaluate(_MESSAGE_COMPOSER_SUBMIT_JS, TARGET) == "invalid"

        await dom_page.set_content(
            _composer(identity=identity, buttons='<button type="submit">stale</button>')
        )
        assert await dom_page.evaluate(_MESSAGE_COMPOSER_FOCUS_JS, TARGET) is True
        state = await dom_page.evaluate(_MESSAGE_COMPOSER_STATE_JS, TARGET)
        await dom_page.locator("form button").evaluate("element => element.remove()")
        submit_target = {**TARGET, "allowEnter": state["submitCount"] == 0}
        assert (
            await dom_page.evaluate(_MESSAGE_COMPOSER_SUBMIT_JS, submit_target)
            == "invalid"
        )

        await dom_page.set_content(_composer(identity=identity))
        assert await dom_page.evaluate(_MESSAGE_COMPOSER_FOCUS_JS, TARGET) is True
        await dom_page.locator('[role="dialog"] > a').evaluate(
            "element => element.setAttribute('href', 'https://www.linkedin.com/in/other/')"
        )
        assert (
            await dom_page.evaluate(_MESSAGE_COMPOSER_SUBMIT_JS, ENTER_TARGET)
            == "invalid"
        )

    async def test_enter_only_when_verified_editor_stays_active_without_buttons(
        self, dom_page
    ):
        await dom_page.set_content(
            _composer(
                identity=(
                    '<span data-profile-urn="urn:li:fsd_profile:ACoAAB">Test</span>'
                )
            )
        )

        assert await dom_page.evaluate(_MESSAGE_COMPOSER_FOCUS_JS, TARGET) is True
        assert (
            await dom_page.evaluate(_MESSAGE_COMPOSER_SUBMIT_JS, ENTER_TARGET)
            == "enter"
        )

        await dom_page.evaluate("document.activeElement.blur()")
        assert (
            await dom_page.evaluate(_MESSAGE_COMPOSER_SUBMIT_JS, ENTER_TARGET)
            == "invalid"
        )
