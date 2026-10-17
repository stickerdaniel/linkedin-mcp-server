# ruff: noqa: F811
"""An actor Save may replace only its action bar before dispatch."""

from unittest.mock import AsyncMock

import pytest

from linkedin_mcp_server.linkedin.navigation import PageNavigator
from linkedin_mcp_server.linkedin.post_actions import (
    POST_ACTION_SIGNALS_JS,
    REFRESH_REACTION_CONTROLS_JS,
    PostActions,
)
from linkedin_mcp_server.linkedin.session import PageSession
from test_post_actions_dom import POST_ID, dom_page  # noqa: F401
from test_post_identity_dom import COMPANY, actor_picker

pytestmark = [pytest.mark.browser_dom, pytest.mark.xdist_group("browser_runtime")]
POST = f"https://www.linkedin.com/feed/update/urn:li:activity:{POST_ID}/"


@pytest.mark.parametrize(
    "change",
    [
        "bar_only",
        "pressed",
        "actor",
        "route",
        "root",
        "duplicate",
        "nested_only",
        "retarget",
        "sdui_retarget",
        "identity_removed",
        "retarget_connected",
        "retarget_at_dispatch",
    ],
)
async def test_actor_bar_remount_before_dispatch_keeps_exact_ownership(
    dom_page, monkeypatch, change
):
    root = await actor_picker(dom_page)
    owner = PostActions(PageSession(dom_page), PageNavigator(PageSession(dom_page)))
    monkeypatch.setattr(owner, "_resolve_actor", AsyncMock(return_value=COMPANY))
    monkeypatch.setattr(
        owner, "_open_post", AsyncMock(return_value=(POST, POST_ID, {}))
    )
    monkeypatch.setattr(owner, "_pin_root", AsyncMock(return_value=root))
    original = PageSession.run_on_linkedin
    remounted = False

    async def intercept(self, program, *args, **kwargs):
        nonlocal remounted
        if (
            program == POST_ACTION_SIGNALS_JS
            and isinstance(args[0], str)
            and not remounted
        ):
            remounted = True
            await root.evaluate(
                """(root, change) => {
              const pin = root.__linkedinMcpPost;
              const next = pin.bar.cloneNode(true);
              const toggle = next.querySelector('button[aria-pressed]');
              toggle.onclick = () => {document.body.dataset.clicks = String(Number(document.body.dataset.clicks || 0)+1); toggle.setAttribute('aria-pressed','true');};
              if(change !== 'retarget_connected') pin.bar.replaceWith(next);
              if(change === 'pressed') toggle.setAttribute('aria-pressed','true');
              if(change === 'actor') pin.actorControl.querySelector('img').src='https://media.licdn.com/dms/image/v2/OTHER/company-logo/x';
              if(change === 'route') history.pushState({}, '', '/feed/update/urn:li:activity:999/');
              if(change === 'root') root.replaceWith(root.cloneNode(true));
              if(change === 'retarget' || change === 'sdui_retarget' || change === 'retarget_connected') root.setAttribute('data-urn','urn:li:ugcPost:999');
              if(change === 'identity_removed') root.removeAttribute('data-urn');
              if(change === 'sdui_retarget' || change === 'identity_removed') {
                root.setAttribute('role','listitem');
                const marker=document.createElement('section'); marker.setAttribute('data-sdui-screen','com.linkedin.sdui.flagshipnav.feed.UpdateDetail'); document.body.append(marker);
                history.replaceState({}, '', '"""
                + POST
                + """');
                pin.route=location.href;
              }
              if(change === 'duplicate') next.after(next.cloneNode(true));
              if(change === 'nested_only') {const nested=document.createElement('section'); nested.setAttribute('data-urn','urn:li:activity:999'); next.replaceWith(nested); nested.append(next);}
            }""",
                change,
            )
        result = await original(self, program, *args, **kwargs)
        if (
            program == REFRESH_REACTION_CONTROLS_JS
            and change == "retarget_at_dispatch"
            and result
        ):
            await root.evaluate(
                "root => root.setAttribute('data-urn','urn:li:ugcPost:999')"
            )
        return result

    monkeypatch.setattr(PageSession, "run_on_linkedin", intercept)
    result = await owner.react_to_post(
        POST, actor=COMPANY["path"], confirm_reaction=True
    )
    if change == "bar_only":
        assert result["status"] == "reacted"
        assert result["acted"] is True
        assert result["retry_safe"] is False
        assert await dom_page.get_attribute("body", "data-clicks") == "1"
    else:
        assert result["acted"] is False
        assert result["retry_safe"] is True
        assert await dom_page.get_attribute("body", "data-clicks") is None
        assert await dom_page.get_attribute("body", "data-clicked") is None
        if change == "pressed":
            assert result["status"] == "already_reacted"
