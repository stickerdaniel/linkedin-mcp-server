"""The browser must not contradict itself, checked rather than remembered.

Everything the identity work rests on was established by hand: a user agent
read off one surface, client hints off another, a frame rate counted against a
control window. None of it is repeatable, and a dependency bump can undo any of
it without a single test going red. That has already happened twice in one
week: the lock moved 148 to 149, and both times the only reason anyone knew the
identity still held was that someone went and measured it again.

**Relations, not values.** Every assertion here compares two things the browser
says about itself. Nothing pins a version, a brand string or a screen size,
because all of those move on their own and a test that pins them is a test
somebody will delete after the third false alarm. What cannot move is that the
page and its workers agree, that the advertised brand major matches the user
agent, and that the window fits the screen it claims to stand on.

**It fails rather than skips where it is meant to gate.** The existing
browser-DOM tests skip when no browser is installed, which is right for them:
they check extraction JS, and running them locally is a convenience. This one
is a gate. A gate that skips itself when the thing it guards is missing is not
a gate, so in CI a missing browser or display is a failure. Locally it still
skips, with a reason that says what to install.

**Two launch modes, because one of them is inert on Linux.**
``hidden_target_is_supported()`` is macOS-only, so the product default
(``headless=True``) is the windowless hidden target on macOS and Chromium's
real headless mode on Linux, where it does announce itself. Asserting "no
headless token" against the default would therefore be an assertion that
catches nothing on the platform CI runs. The headed mode is the one the
container image is heading for, and it is where that assertion bites.
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

import pytest

from linkedin_mcp_server.core.browser import BrowserManager
from browser_identity_harness import IdentityServer, describe_browser

pytestmark = pytest.mark.browser_identity

#: GREASE brands exist to stop anyone parsing the list positionally. They carry
#: deliberately meaningless versions, so a real brand has to be found by
#: excluding them rather than by index.
_GREASE = re.compile(r"not[)/\-_. ]?a[)/\-_. ]?brand", re.IGNORECASE)


def _running_in_ci() -> bool:
    """Whether a missing browser is a failure rather than a reason to skip."""
    return os.environ.get("CI", "").lower() in {"1", "true", "yes"}


def _unavailable(reason: str) -> None:
    """Fail in CI, skip locally. Never quietly pass."""
    if _running_in_ci():
        pytest.fail(
            f"{reason}. In CI this is a failure rather than a skip: this test "
            f"is the only thing that checks the browser still presents one "
            f"coherent identity, and a gate that skips itself is not a gate."
        )
    pytest.skip(f"{reason}; run `uv run patchright install chromium --no-shell`")


async def _describe(tmp_path: Path, *, headless: bool) -> dict:
    profile = tmp_path / f"identity-{'headless' if headless else 'headed'}"
    try:
        manager = BrowserManager(user_data_dir=profile, headless=headless)
        async with manager as browser:
            with IdentityServer() as server:
                described = await describe_browser(browser.page, server.url)
        described["windowless"] = manager._windowless
        return described
    except Exception as exc:  # noqa: BLE001 - the reason is the payload
        _unavailable(f"could not start a browser ({type(exc).__name__}: {exc})")
        raise  # unreachable; _unavailable always raises
    finally:
        shutil.rmtree(profile, ignore_errors=True)


@pytest.fixture(scope="module")
def _mode_results() -> dict:
    """One launch per mode for the whole module, because each costs seconds."""
    return {}


async def _for_mode(tmp_path_factory, cache: dict, headless: bool) -> dict:
    key = "headless" if headless else "headed"
    if key not in cache:
        base = tmp_path_factory.mktemp(f"identity-{key}")
        cache[key] = await _describe(base, headless=headless)
    return cache[key]


@pytest.fixture
async def default_mode(tmp_path_factory, _mode_results) -> dict:
    """The product default: what a user's browser actually is."""
    return await _for_mode(tmp_path_factory, _mode_results, headless=True)


@pytest.fixture
async def headed_mode(tmp_path_factory, _mode_results) -> dict:
    """Headed, which is where the container image is going and where the
    headless token must be absent."""
    return await _for_mode(tmp_path_factory, _mode_results, headless=False)


def _realms(described: dict) -> dict[str, dict]:
    return {
        "page": described["page"],
        "dedicated worker": described["dedicated"],
        "service worker": described["serviceWorker"],
    }


class TestEveryRealmTellsTheSameStory:
    """A contradiction between realms is the cheapest thing for a site to find.

    The user agent is readable from a page, from a dedicated worker and from a
    service worker, and each one also sends it as a request header. That is six
    places one value has to agree with itself. An override that reaches only
    some of them, which is what every user-agent option in this space does, is
    visible to anyone who looks twice.
    """

    async def test_all_three_realms_report_one_user_agent(self, default_mode):
        agents = {name: realm["ua"] for name, realm in _realms(default_mode).items()}

        assert len(set(agents.values())) == 1, agents

    async def test_each_realm_sends_what_it_says(self, default_mode):
        """The JS value and the request header are different channels, and an
        override that changes the string reaches only the first."""
        for name, realm in _realms(default_mode).items():
            assert realm["headers"]["user-agent"] == realm["ua"], name


class TestNothingAnnouncesAutomation:
    async def test_no_headless_token_where_a_window_is_possible(self, headed_mode):
        """Chromium prepends the bare string `Headless` to its product name in
        headless *mode*, so both the user agent and the brand list read
        `HeadlessChrome`. A headed browser must not."""
        for name, realm in _realms(headed_mode).items():
            assert "HeadlessChrome" not in realm["ua"], f"{name}: {realm['ua']}"
        brands = headed_mode["page"]["brands"] or []
        assert not [b for b in brands if "Headless" in b["brand"]], brands

    async def test_the_default_launch_uses_the_hidden_target_where_it_can(
        self, default_mode
    ):
        """Not just self-consistent: actually windowless where that is possible.

        The consistency check below would pass a browser that silently gave up
        on the hidden target and fell back to real headless, because a fallback
        is consistent with itself. That fallback is exactly the regression worth
        catching on the platform most people run, so it gets its own assertion
        against what the code claims the platform can do.
        """
        from linkedin_mcp_server.hidden_target import hidden_target_is_supported

        assert default_mode["windowless"] == hidden_target_is_supported(), (
            "the default launch fell back to Chromium's headless mode on a "
            "platform that supports a hidden target, which puts the "
            "HeadlessChrome token back on every surface"
        )

    async def test_the_default_mode_matches_its_own_claim(self, default_mode):
        """`headless=True` means "no visible window", not "Chromium's headless
        mode". Where a hidden target carries that, the token must be gone; where
        the platform forces real headless the token is expected, and the code
        says which it did."""
        has_token = "HeadlessChrome" in default_mode["page"]["ua"]

        assert has_token is not default_mode["windowless"], (
            f"windowless={default_mode['windowless']} but "
            f"HeadlessChrome present={has_token}"
        )

    async def test_webdriver_is_false_and_unremarkable(self, default_mode):
        assert default_mode["page"]["webdriver"] is False
        descriptor = default_mode["webdriverDescriptor"]
        assert descriptor is not None
        assert descriptor["get"] == "function", descriptor

    async def test_no_automation_globals_in_the_pages_own_realm(self, default_mode):
        """`__pwInitScripts`, `$cdc_*` and friends. Read from the page's own
        script rather than through `page.evaluate()`, which runs somewhere a
        website cannot see."""
        assert default_mode["automationGlobals"] == []


class TestTheHintsAgreeWithTheUserAgent:
    async def test_the_brand_major_matches_the_user_agent_major(self, default_mode):
        """Order is not pinned: the brand list is deliberately shuffled and
        salted with a GREASE entry to stop anyone reading it positionally."""
        page = default_mode["page"]
        ua_major = re.search(r"Chrome/(\d+)", page["ua"])
        assert ua_major, page["ua"]
        real = [b for b in page["brands"] if not _GREASE.search(b["brand"])]

        assert real, page["brands"]
        assert any(b["version"].split(".")[0] == ua_major.group(1) for b in real), (
            f"user agent says {ua_major.group(1)}, brands say "
            f"{[(b['brand'], b['version']) for b in real]}"
        )

    async def test_the_high_entropy_values_are_populated(self, default_mode):
        """`platformVersion` is deliberately not checked: it is normatively
        empty on Linux, so asserting it would fail for a correct browser."""
        hints = default_mode["page"]["highEntropy"]

        assert hints["architecture"], hints
        assert hints["bitness"], hints
        assert hints["fullVersionList"], hints

    async def test_the_header_channel_carries_them_too(self, default_mode):
        """A different channel from `getHighEntropyValues()`, and it only fills
        in on a request *after* the server has asked with `Accept-CH`. The page
        fetches the echo endpoint for exactly that reason.

        Asserted on the page alone: measured, the high-entropy headers do not
        appear on dedicated-worker or service-worker requests even though every
        response carries `Accept-CH`.
        """
        headers = default_mode["page"]["headers"]

        assert headers.get("sec-ch-ua-arch"), headers.get("sec-ch-ua-arch")
        assert headers.get("sec-ch-ua-full-version-list")

    async def test_the_header_brand_major_matches_too(self, default_mode):
        headers = default_mode["page"]["headers"]
        ua_major = re.search(r"Chrome/(\d+)", default_mode["page"]["ua"])
        assert ua_major

        brands = headers.get("sec-ch-ua", "")
        reals = [part for part in brands.split(",") if not _GREASE.search(part)]
        assert reals, brands
        assert any(f'"{ua_major.group(1)}"' in part for part in reals), brands


class TestTheWindowFitsTheScreenItStandsOn:
    async def test_the_outer_window_does_not_exceed_the_screen(self, default_mode):
        """The contradiction this was measured to remove: an emulated viewport
        forced onto a headed window produced an outer height of 805 against a
        screen the same browser reported as 720 tall. Any page can read both."""
        geometry = default_mode["geometry"]

        assert geometry["outerWidth"] <= geometry["screenWidth"], geometry
        assert geometry["outerHeight"] <= geometry["screenHeight"], geometry

    async def test_headed_fits_too(self, headed_mode):
        geometry = headed_mode["geometry"]

        assert geometry["outerWidth"] <= geometry["screenWidth"], geometry
        assert geometry["outerHeight"] <= geometry["screenHeight"], geometry


class TestItIsTheFullBrowser:
    """The headless shell is a different product, and it shows.

    Nothing launches it since the channel became explicit, but the way that
    could come back is a launch option changing under us rather than anyone
    deciding to. These three are what the shell gets wrong.
    """

    async def test_plugins_are_present(self, default_mode):
        assert default_mode["plugins"] > 0

    async def test_the_chrome_object_exists(self, default_mode):
        assert default_mode["hasChromeObject"] == "object"
