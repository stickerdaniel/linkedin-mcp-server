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

Only three cases are about a particular mode: the headless token, whether the
hidden target was used, and whether the default mode matches its own claim.
Everything else is true of a browser however it was started, so it runs against
both, which costs nothing because both launches are already cached. A
regression that appears in headed Chromium alone is otherwise invisible here,
and headed is not the cheaper mode to leave uncovered.
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

import pytest

from linkedin_mcp_server.browser_launch import build_launch_options
from linkedin_mcp_server.config.schema import BrowserConfig
from linkedin_mcp_server.core.browser import BrowserManager
from browser_identity_harness import IdentityServer, describe_browser

#: The group keeps every case in this file on one xdist worker, so the two
#: cached launches are two launches rather than two per worker. It only has an
#: effect under ``--dist loadgroup``, which the CI job passes; without it the
#: mark is inert and the tests still pass, only slower.
pytestmark = [
    pytest.mark.browser_identity,
    pytest.mark.xdist_group("browser_identity"),
]

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
    """Launch the way the product does, and ask the browser what it is.

    The options come from ``build_launch_options`` rather than being written
    out here, because a gate that assembles its own launch measures a browser
    nobody ships. Measured, and the reason this is not a stylistic preference:
    constructing ``BrowserManager`` bare leaves out ``channel="chromium"``, so
    on Linux, where the default mode is physically headless, Playwright
    resolves the binary from that flag alone and asks for the headless shell
    the setup no longer installs. Every default-mode case then skips itself
    while the shipped configuration is fine.

    ``BrowserConfig()`` rather than ``get_config()``: the global parses
    ``sys.argv`` and aborts under pytest, and the defaults are what the gate is
    about anyway.

    **Only the launch is allowed to turn into a skip.** A browser that cannot
    start is a missing dependency; a browser that starts and then fails to
    answer is the regression this file exists to catch, and routing both
    through the same handler would let the second hide behind the first
    wherever a skip is still permitted.
    """
    launch_options, viewport = build_launch_options(BrowserConfig())
    profile = tmp_path / f"identity-{'headless' if headless else 'headed'}"
    manager = BrowserManager(
        user_data_dir=profile,
        headless=headless,
        viewport=viewport,
        **launch_options,
    )
    try:
        try:
            await manager.start()
        except Exception as exc:  # noqa: BLE001 - the reason is the payload
            _unavailable(f"could not start a browser ({type(exc).__name__}: {exc})")
            raise  # unreachable; _unavailable always raises

        try:
            with IdentityServer() as server:
                described = await describe_browser(manager.page, server.url)
            # Read before the close, because ``_no_window_available`` is set
            # during the launch and nothing after it changes the answer.
            described["windowless"] = manager._windowless
        finally:
            # Closed by name rather than through ``async with``, so the browser
            # is torn down on the measurement's failure as well as its success.
            #
            # Its verdict is deliberately not asserted. ``close()`` bounds each
            # cleanup step at ten seconds and reports ``False`` when one runs
            # out, which is the bound working rather than a browser misbehaving:
            # measured on a machine at load average 96, a headed teardown missed
            # it five times in eight, while an interleaved A/B against a plain
            # persistent context put the two within 0.3 s of each other, so
            # there is nothing here that identity work could regress. Asserting
            # it would report machine load as a fingerprint failure. The
            # shutdown contract itself is covered where it can be made
            # deterministic, in ``test_core_browser.py`` and
            # ``test_browser_driver.py``, by shrinking that bound.
            await manager.close()

        return described
    finally:
        shutil.rmtree(profile, ignore_errors=True)


@pytest.fixture(scope="module")
def _mode_results() -> dict:
    """One launch per mode for the whole module, because each costs seconds."""
    return {}


async def _for_mode(tmp_path_factory, cache: dict, headless: bool) -> dict:
    """The cached description of one launch, successful or not.

    The failure is cached too, which is not symmetry for its own sake. Thirteen
    cases read the default launch and twelve read the headed one, so a mode
    that fails would otherwise be attempted once per case: a browser that will
    not start costs a timeout each time, and a run whose real answer is "this
    browser is broken" spends a quarter of an hour finding that out twelve more
    times.

    ``BaseException`` because the outcomes worth remembering are the ones
    pytest raises. ``pytest.fail`` and ``pytest.skip`` both raise from
    ``OutcomeException``, which does not descend from ``Exception``, and either
    one re-raised for the remaining cases is exactly the right answer for them.
    """
    key = "headless" if headless else "headed"
    if key not in cache:
        base = tmp_path_factory.mktemp(f"identity-{key}")
        try:
            cache[key] = (None, await _describe(base, headless=headless))
        except BaseException as exc:
            cache[key] = (exc, None)
            raise

    failure, described = cache[key]
    if failure is not None:
        raise failure
    return described


@pytest.fixture
async def default_mode(tmp_path_factory, _mode_results) -> dict:
    """The product default: what a user's browser actually is."""
    return await _for_mode(tmp_path_factory, _mode_results, headless=True)


@pytest.fixture
async def headed_mode(tmp_path_factory, _mode_results) -> dict:
    """Headed, which is where the container image is going and where the
    headless token must be absent."""
    return await _for_mode(tmp_path_factory, _mode_results, headless=False)


@pytest.fixture(params=[True, False], ids=["default", "headed"])
async def either_mode(request, tmp_path_factory, _mode_results) -> dict:
    """Both launches, for every relation that does not depend on the mode.

    Most of what this file asserts is true of a browser regardless of how it
    was started, and running it against one mode only halves the gate for free:
    a regression that shows up in headed Chromium alone would leave the two
    headed assertions and every default-mode assertion untouched. Headed is
    also the mode the container image is heading for, so it is not the cheaper
    one to leave uncovered.

    No extra launches. Both modes are already started and cached for the module,
    so this only decides which of the two an existing case reads.
    """
    return await _for_mode(tmp_path_factory, _mode_results, headless=request.param)


def _structured_string(value: str | None) -> str | None:
    """The text inside a structured-field string, or None if there is none.

    Client hints arrive quoted: ``Sec-CH-UA-Arch: "arm"``. So a hint the browser
    left blank arrives as two quote characters, which is a perfectly truthy
    Python string, and ``assert headers.get(...)`` accepts it. Blank is exactly
    the failure worth catching here, since it is what a browser emits when it
    has the value in JavaScript and cannot put it on the wire.
    """
    if value is None:
        return None
    return value.strip().strip('"') or None


def _realms(described: dict) -> dict[str, dict]:
    return {
        "page": described["page"],
        "dedicated worker": described["dedicated"],
        "service worker": described["serviceWorker"],
        "cross-origin iframe": described["iframe"],
    }


class TestEveryRealmTellsTheSameStory:
    """A contradiction between realms is the cheapest thing for a site to find.

    The user agent is readable from a page, from a dedicated worker, from a
    service worker and from a frame on another origin, and each one also sends
    it as a request header. That is eight places one value has to agree with
    itself. An override that reaches only some of them, which is what every
    user-agent option in this space does, is visible to anyone who looks twice:
    ``user_agent=`` never reaches a service worker at all, and the frame is the
    other surface it routinely misses.
    """

    async def test_every_realm_reports_one_user_agent(self, either_mode):
        agents = {name: realm["ua"] for name, realm in _realms(either_mode).items()}

        assert len(set(agents.values())) == 1, agents

    async def test_each_realm_sends_what_it_says(self, either_mode):
        """The JS value and the request header are different channels, and an
        override that changes the string reaches only the first."""
        for name, realm in _realms(either_mode).items():
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
        """The hidden target was used, and not quietly given up on.

        ``_windowless`` is not merely the platform predicate restated. Its
        third term is ``not self._no_window_available``, which the launch sets
        when a headed browser is refused and the fallback to Chromium's real
        headless mode runs instead. A fallback is consistent with itself, so
        every other assertion here would pass it; this is the one that does
        not.

        What it does *not* prove is that no window is on screen. That takes a
        window-server query, and the two ways the page could be the wrong one
        -- a hidden target identified by position rather than by its nonce, or
        a startup page that is never closed -- are covered directly in
        ``tests/test_hidden_target.py``.
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

    async def test_webdriver_is_false_and_unremarkable(self, either_mode):
        """`false` is not enough on its own: how it got there is also readable.

        A native ``Navigator.prototype.webdriver`` is an accessor with a getter,
        no setter, and configurable. Redefining it to return ``false`` is the
        usual way to hide automation, and the descriptor does not give that
        away: ``Object.defineProperty`` on an existing configurable property
        leaves every attribute the caller did not name exactly as it was, so
        the shape still matches. Measured, against exactly that mutation.

        The getter's source does give it away. A native accessor stringifies to
        ``[native code]``; anything written in JavaScript stringifies to itself.
        """
        assert either_mode["page"]["webdriver"] is False
        descriptor = either_mode["webdriverDescriptor"]
        assert descriptor is not None
        assert descriptor["get"] == "function", descriptor
        assert descriptor["set"] == "undefined", descriptor
        assert descriptor["configurable"] is True, descriptor
        assert "[native code]" in descriptor["getSource"], descriptor

    async def test_no_automation_globals_in_the_pages_own_realm(self, either_mode):
        """`__pwInitScripts`, `$cdc_*` and friends. Read from the page's own
        script rather than through `page.evaluate()`, which runs somewhere a
        website cannot see."""
        assert either_mode["automationGlobals"] == []


class TestTheHintsAgreeWithTheUserAgent:
    async def test_the_brand_major_matches_the_user_agent_major(self, either_mode):
        """Order is not pinned: the brand list is deliberately shuffled and
        salted with a GREASE entry to stop anyone reading it positionally."""
        page = either_mode["page"]
        ua_major = re.search(r"Chrome/(\d+)", page["ua"])
        assert ua_major, page["ua"]
        real = [b for b in page["brands"] if not _GREASE.search(b["brand"])]

        assert real, page["brands"]
        assert any(b["version"].split(".")[0] == ua_major.group(1) for b in real), (
            f"user agent says {ua_major.group(1)}, brands say "
            f"{[(b['brand'], b['version']) for b in real]}"
        )

    async def test_the_high_entropy_values_are_populated(self, either_mode):
        """`platformVersion` is deliberately not checked: it is normatively
        empty on Linux, so asserting it would fail for a correct browser."""
        hints = either_mode["page"]["highEntropy"]

        assert hints["architecture"], hints
        assert hints["bitness"], hints
        assert hints["fullVersionList"], hints

    async def test_the_header_channel_carries_them_too(self, either_mode):
        """A different channel from `getHighEntropyValues()`, and it only fills
        in on a request *after* the server has asked with `Accept-CH`. The page
        fetches the echo endpoint for exactly that reason.

        Asserted on the page alone: measured, the high-entropy headers do not
        appear on dedicated-worker or service-worker requests even though every
        response carries `Accept-CH`.

        Compared against the JavaScript channel rather than merely required to
        be present, because "present" is the one thing a blank hint also is.
        Two channels agreeing is the relation worth holding; it is also what a
        browser fails when it knows the value and cannot put it on the wire.
        """
        headers = either_mode["page"]["headers"]
        hints = either_mode["page"]["highEntropy"]

        for header, js in (("arch", "architecture"), ("bitness", "bitness")):
            sent = _structured_string(headers.get(f"sec-ch-ua-{header}"))
            assert sent, f"sec-ch-ua-{header} = {headers.get(f'sec-ch-ua-{header}')!r}"
            assert sent == hints[js], (
                f"sec-ch-ua-{header} says {sent!r} and getHighEntropyValues "
                f"says {hints[js]!r}"
            )

        assert headers.get("sec-ch-ua-full-version-list")

    async def test_the_header_brand_major_matches_too(self, either_mode):
        headers = either_mode["page"]["headers"]
        ua_major = re.search(r"Chrome/(\d+)", either_mode["page"]["ua"])
        assert ua_major

        brands = headers.get("sec-ch-ua", "")
        reals = [part for part in brands.split(",") if not _GREASE.search(part)]
        assert reals, brands
        assert any(f'"{ua_major.group(1)}"' in part for part in reals), brands


class TestTheWindowFitsTheScreenItStandsOn:
    async def test_the_outer_window_does_not_exceed_the_screen(self, either_mode):
        """The contradiction this was measured to remove: an emulated viewport
        forced onto a headed window produced an outer height of 805 against a
        screen the same browser reported as 720 tall. Any page can read both."""
        geometry = either_mode["geometry"]

        assert geometry["outerWidth"] <= geometry["screenWidth"], geometry
        assert geometry["outerHeight"] <= geometry["screenHeight"], geometry


class TestItIsTheFullBrowser:
    """The headless shell is a different product, and it shows.

    Nothing launches it since the channel became explicit, but the way that
    could come back is a launch option changing under us rather than anyone
    deciding to. These three are what the shell gets wrong.
    """

    async def test_plugins_are_present(self, either_mode):
        assert either_mode["plugins"] > 0

    async def test_the_chrome_object_exists(self, either_mode):
        assert either_mode["hasChromeObject"] == "object"
