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
#:
#: The separator class has to be wide. Chromium draws it from a fixed set that
#: includes ``;``, ``:``, ``(``, ``=`` and ``?`` alongside the obvious ones, and
#: two of those are in play right here: the bundled browser sends
#: ``Not)A;Brand`` and Chrome 151 under ``CHROME_PATH`` sends ``Not=A?Brand``.
#: An earlier spelling of this pattern listed five characters and matched
#: neither, quietly promoting the GREASE entry to a real brand. ``[\W_]`` covers
#: the whole set, since ``_`` is the one separator that is also a word
#: character.
#:
#: This is the one place the file names a value rather than a relation, and it
#: is a considered trade rather than an oversight. The specification does not
#: require the words: a future Chromium could send ``Fake?Browser`` instead,
#: and the brand-major cases would go red on a coherent browser. What that buys
#: is ``all`` in place of ``any``. Without a way to name the fake entry, the
#: strongest available rule is "at least one brand agrees with the user agent",
#: and that passes a list carrying ``Chromium`` at 149 beside ``Google Chrome``
#: at 150 -- a browser contradicting itself in public, which is precisely what
#: is being looked for. Identifying it structurally instead would have to
#: assume how many fake entries there are, and the specification does not fix
#: that either. So: a rename costs one red that names the brands it saw and is
#: repaired by editing this line, and the alternative costs a real
#: contradiction going unseen. If the wording ever does move, that is the
#: failure to expect and this comment is the answer to it.
_GREASE = re.compile(r"not[\W_]?a[\W_]?brand", re.IGNORECASE)

#: The NativeFunction form ECMA-262 gives ``Function.prototype.toString`` for a
#: built-in. Written as a grammar rather than as today's exact string, because
#: the whitespace between the tokens is the engine's choice while the tokens
#: themselves are not. What it refuses is anything carrying a function body,
#: which is every accessor written in JavaScript.
_NATIVE_FUNCTION = re.compile(
    r"function\s+((get|set)\s+)?\S*\s*\(\s*\)\s*\{\s*\[\s*native\s+code\s*\]\s*\}"
)

#: What the user agent has to say for a platform the client hint claims. Only
#: the ones this browser can currently report, and a platform that is not here
#: is not judged: a new one belongs in this table after somebody looks it up,
#: rather than failing a browser that is telling the truth about itself.
#: The ones among them that no mobile device runs. Kept beside the table
#: rather than derived from it, because "not mobile" is a claim about the
#: platform and not about whether its user-agent token is known.
_DESKTOP_PLATFORMS = frozenset(
    {"macOS", "Windows", "Linux", "Chrome OS", "Chromium OS"}
)

#: The realms with a ``Navigator`` rather than a ``WorkerNavigator``. Both are
#: documents and both are asked the same questions; a frame is a realm of its
#: own, and a patch applied only there is invisible from the top.
_DOCUMENT_REALMS = ("page", "cross-origin iframe")

_PLATFORM_TOKENS = {
    "macOS": "Macintosh",
    "Windows": "Windows",
    "Linux": "Linux",
    "Android": "Android",
    "Chrome OS": "CrOS",
    "Chromium OS": "CrOS",
}


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
            # Whether this machine turned out to have nowhere to put a window,
            # which is a fact about the machine rather than about the browser.
            described["noWindowAvailable"] = manager._no_window_available
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

    The failure is cached too, which is not symmetry for its own sake. Most of
    the cases in this file read one launch or the other, so a mode that fails
    would otherwise be attempted once per case: a browser that will not start
    costs a timeout each time, and a run whose real answer is "this browser is
    broken" would spend a quarter of an hour finding that out again and again.
    Measured against a launch made to fail every time: one attempt with this,
    twelve without.

    ``BaseException`` because the outcomes worth remembering are the ones
    pytest raises: ``pytest.fail`` and ``pytest.skip`` both raise from
    ``OutcomeException``, which does not descend from ``Exception``.

    What is kept is a description rather than the exception object. Re-raising
    the instance works, and it also carries its traceback, its context and the
    frame locals of the launch that failed, so every replay appends another
    stack to the same chain and pins the first ``BrowserManager`` alive until
    the module ends. The first case to run reports the real failure in full;
    the rest need only to be told that it already happened.
    """
    key = "headless" if headless else "headed"
    if key not in cache:
        base = tmp_path_factory.mktemp(f"identity-{key}")
        try:
            cache[key] = (None, await _describe(base, headless=headless))
        except BaseException as exc:
            skipped = isinstance(exc, pytest.skip.Exception)
            cache[key] = ((skipped, f"{type(exc).__name__}: {exc}"), None)
            raise

    failure, described = cache[key]
    if failure is not None:
        skipped, reason = failure
        already = f"the {key} launch already failed in this module ({reason})"
        pytest.skip(already) if skipped else pytest.fail(already)
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


def _split_outside_quotes(value: str, separator: str) -> list[str]:
    """Split on *separator* where it is not inside a quoted string.

    Not a general structured-fields parser, and it does not pretend to be one:
    it knows that a quoted string runs to its unescaped closing quote and that
    ``\\`` escapes the next character, which is all RFC 8941 allows inside one.
    That is enough for the two headers read here, and it is the part that
    matters, because a plain ``split`` on the separator would cut a brand name
    containing it in half and fail a browser that is telling the truth.
    """
    parts: list[str] = []
    current: list[str] = []
    quoted = escaped = False
    for character in value:
        if escaped:
            escaped = False
        elif quoted and character == "\\":
            escaped = True
        elif character == '"':
            quoted = not quoted
        elif character == separator and not quoted:
            parts.append("".join(current))
            current = []
            continue
        current.append(character)
    if quoted or escaped:
        raise ValueError(f"an unterminated structured string: {value!r}")
    parts.append("".join(current))
    return parts


def _structured_string(value: str | None) -> str | None:
    """The text inside a structured-field string, or None if there is none.

    Client hints arrive quoted: ``Sec-CH-UA-Arch: "arm"``. So a hint the browser
    left blank arrives as two quote characters, which is a perfectly truthy
    Python string, and ``assert headers.get(...)`` accepts it. Blank is exactly
    the failure worth catching here, since it is what a browser emits when it
    has the value in JavaScript and cannot put it on the wire.

    Backslash escapes are undone rather than left in place, so a brand carrying
    a quote reads as the browser meant it and not as its wire spelling.

    An unquoted value is not read leniently as the text it resembles. Every
    field this parses is an ``sf-string``, so ``Sec-CH-UA-Arch: arm`` is not a
    differently spelled ``"arm"`` but a header a conforming recipient rejects,
    and treating the two alike would let a browser emitting invalid syntax pass
    as one emitting valid syntax.
    """
    if value is None:
        return None
    token = value.strip()
    if len(token) < 2 or not token.startswith('"') or not token.endswith('"'):
        return None
    return _unescape(token[1:-1]) or None


def _unescape(text: str) -> str:
    """Undo the two escapes RFC 8941 allows inside a string, and only those.

    A backslash may precede a quote or another backslash; anything else, and a
    backslash at the very end, is a parse failure rather than a character to
    pass through. Accepting them would read ``"a\\rm"`` as ``arm``, so a browser
    emitting a hint no conforming recipient would take compared equal to one
    emitting a valid one.
    """
    out: list[str] = []
    escaped = False
    for character in text:
        if escaped:
            if character not in '"\\':
                raise ValueError(f"invalid escape in a structured string: {text!r}")
            out.append(character)
            escaped = False
        elif character == "\\":
            escaped = True
        elif character == '"':
            # Every quote inside the string has to be escaped, and this is the
            # place that can tell: looking for an escape *somewhere* in the
            # value, as an earlier version did, accepts one escaped quote as
            # cover for a second unescaped one.
            raise ValueError(f"an unescaped quote in a structured string: {text!r}")
        else:
            out.append(character)
    if escaped:
        raise ValueError(f"a structured string ends in a backslash: {text!r}")
    return "".join(out)


def _brand_list(value: str | None) -> list[tuple[str, str | None]]:
    """A ``"brand";v="version"`` list as sorted pairs, empty if there is none.

    Sorted rather than left in order, because the list is shuffled on purpose
    and reading it positionally is the mistake it exists to prevent. Pairs
    rather than a mapping, because a mapping silently keeps only the last
    version of a repeated brand: a list carrying ``Chromium`` at 148 *and* at
    149 collapses to the current one, and a browser publishing two versions of
    itself at once is exactly the contradiction being looked for.

    A member that is not a quoted string raises rather than being skipped. An
    empty member and a bare token are both parse failures under RFC 8941, and
    dropping them would turn a browser emitting invalid syntax into one that
    looks like it emitted the valid list the comparison expects. A repeated
    ``v`` is not an error: later parameters overwrite earlier ones, which is
    what the specification says to do.
    """
    if not value:
        return []
    listed: list[tuple[str, str | None]] = []
    for item in _split_outside_quotes(value, ","):
        fields = _split_outside_quotes(item.strip(), ";")
        name = _structured_string(fields[0])
        if name is None:
            raise ValueError(f"not a structured-fields list member: {item!r}")
        version = None
        for parameter in fields[1:]:
            key, _, raw = parameter.partition("=")
            if not key.strip():
                raise ValueError(f"an empty parameter on a list member: {item!r}")
            if key.strip() == "v":
                version = _structured_string(raw)
        listed.append((name, version))
    return sorted(listed)


def _js_brand_list(entries: list[dict]) -> list[tuple[str, str | None]]:
    """The JavaScript side of the same list, in the same shape."""
    return sorted((entry["brand"], entry["version"]) for entry in entries)


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

    async def test_the_frame_is_told_the_same_about_the_browser(self, either_mode):
        """The low-entropy hints reach another origin, and must not change on
        the way.

        The user agent was the only thing compared across realms, so an
        injection that touched one origin's client hints and left its user
        agent alone passed every case here. These three are the hints Chromium
        sends to a third party without being asked, measured identical to the
        page's; the high-entropy ones are not compared, because measurement
        says they do not reach a worker at all and delegation to a frame is the
        embedder's choice rather than a property of the browser.
        """
        page = either_mode["page"]["headers"]
        frame = either_mode["iframe"]["headers"]

        # Parsed, not compared as text. The brand list is deliberately shuffled
        # and the specification asks for the order to move over time, so two
        # orderings of the same pairs are the same answer; comparing the raw
        # strings would call that a contradiction.
        assert _brand_list(frame["sec-ch-ua"]) == _brand_list(page["sec-ch-ua"]), (
            f"sec-ch-ua reads {frame['sec-ch-ua']!r} in the frame and "
            f"{page['sec-ch-ua']!r} on the page"
        )
        for hint in ("sec-ch-ua-mobile", "sec-ch-ua-platform"):
            assert frame[hint] == page[hint], (
                f"{hint} reads {frame.get(hint)!r} in the frame and "
                f"{page.get(hint)!r} on the page"
            )

    async def test_the_platform_and_form_factor_agree_with_javascript(
        self, either_mode
    ):
        """Two hints that were compared between origins and nowhere else.

        Agreeing across origins says only that whatever is being reported is
        reported consistently, so a change that reached both said nothing: a
        browser sending ``"Windows"`` and ``?1`` from a machine whose user
        agent says Macintosh passed every case. The JavaScript side is the
        other channel, and it is the one that has to match.

        The user agent is brought in only where the platform is one this knows
        a token for. An unfamiliar platform is left alone rather than guessed
        at, because a new one is a thing to look up and not a reason to fail a
        browser that is telling the truth.
        """
        page = either_mode["page"]
        headers = page["headers"]

        assert _structured_string(headers["sec-ch-ua-platform"]) == page["platform"], (
            f"sec-ch-ua-platform says {headers['sec-ch-ua-platform']!r} and "
            f"userAgentData says {page['platform']!r}"
        )
        assert headers["sec-ch-ua-mobile"] == ("?1" if page["mobile"] else "?0"), (
            f"sec-ch-ua-mobile says {headers['sec-ch-ua-mobile']!r} and "
            f"userAgentData.mobile is {page['mobile']!r}"
        )

        # Agreeing with itself is not enough for this one either. A browser
        # reporting macOS and a mobile form factor at the same time is saying
        # two things no device is, and it passed while only the two channels
        # were held against each other. Judged only for platforms known to be
        # desktop, on the same principle as the table above.
        if page["platform"] in _DESKTOP_PLATFORMS:
            assert page["mobile"] is False, (
                f"the platform is {page['platform']!r} and the browser calls "
                f"itself mobile"
            )

        token = _PLATFORM_TOKENS.get(page["platform"])
        if token is not None:
            assert token in page["ua"], (
                f"the platform is {page['platform']!r} and the user agent "
                f"({page['ua']}) does not mention {token!r}"
            )

    async def test_the_two_brand_lists_name_the_same_browser(self, either_mode):
        """The short list and the full one describe one browser, or should.

        Each was tied to the user agent's major and to its own counterpart on
        the wire, and never to the other. So ``brands`` could name ``Chromium``
        while ``fullVersionList`` named something else entirely at the same
        version, on both channels, and every case passed. The names have to be
        the same names, and each short version has to be the major of the full
        one it belongs to.
        """
        page = either_mode["page"]
        short = _js_brand_list(page["brands"])
        full = _js_brand_list(page["highEntropy"]["fullVersionList"])

        assert [brand for brand, _ in short] == [brand for brand, _ in full], (
            f"brands names {[b for b, _ in short]} and fullVersionList names "
            f"{[b for b, _ in full]}"
        )
        assert short == [
            (brand, (version or "").split(".")[0]) for brand, version in full
        ], f"brands says {short} and fullVersionList says {full}"


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

        if default_mode["noWindowAvailable"]:
            # A supported configuration, not a regression: macOS reached over
            # SSH or run from a launch daemon has no WindowServer, the headed
            # attempt is refused, and the fallback to real headless is the
            # right answer. Routed through the same handler as a missing
            # browser, so it skips on the developer's machine and still fails
            # in CI, where every runner does have somewhere to put a window.
            _unavailable("this machine has nowhere to put a window")

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

        The getter's source does give it away, but only if the whole of it is
        read. Looking for ``[native code]`` as a substring is not enough:
        ``function webdriver() { /* [native code] */ return false; }`` contains
        it and passes, measured in Chrome 151. What a native accessor produces
        is the NativeFunction form ECMA-262 specifies, and nothing written in
        JavaScript can be spelled that way.

        Held against ``userAgent`` rather than a literal, because that is a
        native accessor on the same prototype that nothing has a reason to
        touch. If V8 changes how it stringifies built-ins, both change together
        and this survives; pinning the spelling is what would not.

        The control is checked as well, or two accessors patched together would
        agree with each other and say nothing. Against the NativeFunction
        grammar from ECMA-262 rather than against today's exact spelling, so it
        holds whatever whitespace an engine chooses while still refusing
        anything with a function body in it.

        And the prototype is not the only place to look. Defining ``webdriver``
        directly on ``navigator`` shadows the accessor without touching it, so
        the value reads false while every attribute here still measures native.
        Measured in the bundled browser: a real browser has no own property.

        **Where this stops, stated rather than papered over.** Everything above
        is read from inside the page's realm, so a patch applied to that whole
        realm can answer every question consistently: replace the getter, patch
        ``Function.prototype.toString`` to describe both it and itself as
        native, delegate for everything else, and each assertion here is
        satisfied. Measured, and one more layer of self-description does not
        close it, because the layer is asked the same way. Two escapes were
        tried and neither works: the isolated world ``page.evaluate`` uses
        shares these prototypes, so it reads the patch back unchanged; and a
        CDP read still has to evaluate ``getOwnPropertyDescriptor`` in the
        realm, which is as patchable as the rest.

        That boundary is the realm's, not this file's. The standard here is
        that nothing the browser says is refuted by another surface of the same
        browser, and a uniformly patched realm is coherent -- dishonest, but
        coherent, and invisible to the websites this exists to reason about.
        Proving the runtime unmodified is a different exercise from proving it
        consistent, and it wants a different tool.
        """
        realms = _realms(either_mode)
        for name in _DOCUMENT_REALMS:
            realm = realms[name]
            accessors = realm["accessors"]

            assert realm["webdriver"] is False, f"{name}: {realm['webdriver']!r}"
            assert accessors["ownWebdriver"] is False, (
                f"{name}: navigator carries its own webdriver property, which "
                f"shadows the prototype accessor and leaves it looking untouched"
            )

            descriptor = accessors["webdriverDescriptor"]
            native = accessors["userAgentDescriptor"]
            assert descriptor is not None and native is not None, name
            assert _NATIVE_FUNCTION.fullmatch(accessors["toStringSource"]), (
                f"{name}: Function.prototype.toString is not itself native, so "
                f"nothing it says about any other function counts: "
                f"{accessors['toStringSource']!r}"
            )
            assert _NATIVE_FUNCTION.fullmatch(native["getSource"]), (
                f"{name}: the control accessor is not native either: "
                f"{native['getSource']!r}"
            )

            flags = ("get", "set", "enumerable", "configurable")
            assert {key: descriptor[key] for key in flags} == {
                key: native[key] for key in flags
            }, f"{name}: webdriver {descriptor} against userAgent {native}"

            assert descriptor["getSource"] == native["getSource"].replace(
                "userAgent", "webdriver"
            ), (
                f"{name}: the webdriver getter reads "
                f"{descriptor['getSource']!r} where a native one on this "
                f"prototype reads {native['getSource']!r}"
            )

    async def test_no_realm_admits_to_being_driven(self, either_mode):
        """`navigator.webdriver` is readable in more than one place.

        Only the top document was asked, so a frame on another origin could
        report ``true`` while all twenty-seven cases passed. Measured, and the
        reason the two kinds are separated rather than looped over uniformly:
        ``WorkerNavigator`` has no ``webdriver`` attribute at all, so both
        workers report undefined and a check written as "false everywhere"
        would be satisfied by a browser that had simply dropped the property.
        Each realm answers whether the attribute is *there*, rather than the
        test reading its absence out of a missing JSON key. Those are not the
        same question: ``JSON.stringify`` drops a key whose value is undefined,
        so an attribute defined as undefined looks exactly like one that was
        never defined, while a website asking ``'webdriver' in navigator``
        sees the difference.
        """
        realms = _realms(either_mode)

        for name, realm in realms.items():
            if name in _DOCUMENT_REALMS:
                assert realm["hasWebdriver"] is True, f"{name} has no webdriver"
                assert realm["webdriver"] is False, (
                    f"{name} reports webdriver={realm['webdriver']!r}"
                )
            else:
                assert realm["hasWebdriver"] is False, (
                    f"{name} grew a webdriver attribute, which no WorkerNavigator has"
                )

    async def test_no_automation_globals_in_the_pages_own_realm(self, either_mode):
        """`__pwInitScripts`, `$cdc_*` and friends. Read from the page's own
        script rather than through `page.evaluate()`, which runs somewhere a
        website cannot see."""
        assert either_mode["automationGlobals"] == []


class TestTheHintsAgreeWithTheUserAgent:
    async def test_the_brand_major_matches_the_user_agent_major(self, either_mode):
        """Order is not pinned: the brand list is deliberately shuffled and
        salted with a GREASE entry to stop anyone reading it positionally.

        Every real brand has to agree, not one of them. Measured: Chrome for
        Testing advertises ``Chromium`` alone, and Chrome 151 under
        ``CHROME_PATH`` advertises ``Google Chrome`` and ``Chromium`` at the
        same major. A list carrying one brand at the user agent's major and
        another at a different one is a contradiction the browser is publishing
        about itself, and ``any`` would read the agreeing half and call it
        settled.
        """
        page = either_mode["page"]
        ua_major = re.search(r"Chrome/(\d+)", page["ua"])
        assert ua_major, page["ua"]
        real = [b for b in page["brands"] if not _GREASE.search(b["brand"])]

        assert real, page["brands"]
        assert all(b["version"].split(".")[0] == ua_major.group(1) for b in real), (
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

    async def test_the_full_versions_name_the_running_major(self, either_mode):
        """Non-empty is not the same as current.

        The two channels are compared against each other below, so a version
        list that has gone stale in *both* agrees with itself perfectly: user
        agent and low-entropy brands at 149 while every full version still says
        148 is a browser contradicting itself about its own build, and nothing
        else here looks at it. Tying the real entries to the user agent's major
        is what closes that, and it disposes of a GREASE-only list at the same
        time, since after the filter there would be nothing left to check.
        """
        page = either_mode["page"]
        ua_major = re.search(r"Chrome/(\d+)", page["ua"])
        assert ua_major, page["ua"]
        real = [
            pair
            for pair in _js_brand_list(page["highEntropy"]["fullVersionList"])
            if not _GREASE.search(pair[0])
        ]

        assert real, page["highEntropy"]["fullVersionList"]
        assert all(
            (version or "").split(".")[0] == ua_major.group(1) for _, version in real
        ), f"user agent says {ua_major.group(1)}, full versions say {real}"

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

        # The version list gets the same treatment rather than a presence
        # check, which a stale or GREASE-only list would also satisfy. The
        # contradiction worth catching is a wire header still naming the
        # previous build while JavaScript names the current one, and only a
        # comparison sees that.
        sent_brands = _brand_list(headers.get("sec-ch-ua-full-version-list"))
        assert sent_brands, headers.get("sec-ch-ua-full-version-list")
        assert sent_brands == _js_brand_list(hints["fullVersionList"]), (
            f"sec-ch-ua-full-version-list says {sent_brands} and "
            f"getHighEntropyValues says {hints['fullVersionList']}"
        )

    async def test_the_header_brand_list_matches_the_one_in_javascript(
        self, either_mode
    ):
        """The low-entropy list is readable twice, and the two must be one list.

        Both were checked against the user agent's major and never against each
        other, which leaves the brand *names* unbound: JavaScript can say
        ``Chromium`` while the wire says something else entirely, at the same
        major, and every other assertion here is satisfied. That is the shape a
        header override takes, and it is one of the two surfaces such an
        override is known to miss.
        """
        listed = _brand_list(either_mode["page"]["headers"].get("sec-ch-ua"))

        assert listed == _js_brand_list(either_mode["page"]["brands"]), (
            f"sec-ch-ua says {listed} and navigator.userAgentData says "
            f"{either_mode['page']['brands']}"
        )

    # There is no separate case checking the wire brand major against the user
    # agent. It was here and it caught nothing once the two lists above had to
    # be equal: the header list equals the JavaScript one, and the JavaScript
    # one has to match the user agent, so the wire relation follows and a case
    # asserting it can only fail alongside one of those two. This file's rule
    # is that an assertion which catches nothing is not kept for the look of it.


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
