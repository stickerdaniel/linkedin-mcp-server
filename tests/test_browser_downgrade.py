"""What happens when the browser is older than the profile it is handed.

Chromium records its version in ``Last Version`` on every run and carries on
regardless when an older binary opens the same profile, leaving each store to
decide for itself whether it can still be read. A store that says no is not
reported: the browser simply runs without it. The cookie store is one, so the
user-visible symptom of the case these tests describe is a working session
disappearing.
"""

import asyncio
import logging
import os
from pathlib import Path
from unittest import mock

import pytest

from linkedin_mcp_server.browser_downgrade import (
    LAST_VERSION_FILE,
    BrowserBuild,
    a_version_can_be_asked_for,
    is_comparable,
    parse_build,
    parse_version,
    profile_was_written_by,
    refuse_a_downgrade,
    version_of,
)
from linkedin_mcp_server.core.browser import BrowserManager
from linkedin_mcp_server.exceptions import BrowserDowngradeError


def _shell_wrapper(tmp_path: Path, body: str) -> Path:
    """An executable standing in for a ``CHROME_PATH`` that is not a browser.

    Not hypothetical: pointing ``CHROME_PATH`` at a launcher script is the
    documented way to run a browser with extra arguments or under a sandbox.
    """
    wrapper = tmp_path / "chrome-wrapper.sh"
    wrapper.write_text(f"#!/bin/sh\n{body}\n")
    wrapper.chmod(0o755)
    return wrapper


def _profile_last_opened_by(tmp_path: Path, version: str) -> Path:
    profile = tmp_path / "profile"
    profile.mkdir(parents=True, exist_ok=True)
    # No trailing newline, matching what Chromium writes.
    (profile / LAST_VERSION_FILE).write_text(version)
    return profile


class TestReadingAVersion:
    """One parser for two sources, because they disagree in shape.

    ``--version`` prints a product name ahead of the number; ``Last Version``
    holds the bare number with no newline.
    """

    @pytest.mark.parametrize(
        "text, expected",
        [
            ("Google Chrome for Testing 148.0.7778.96\n", (148, 0, 7778, 96)),
            ("Chromium 148.0.7778.96\n", (148, 0, 7778, 96)),
            ("Google Chrome 150.0.7871.187\n", (150, 0, 7871, 187)),
            ("150.0.7871.187", (150, 0, 7871, 187)),
        ],
    )
    def test_reads_the_real_shapes(self, text, expected):
        assert parse_version(text) == expected

    def test_ignores_a_build_suffix(self):
        """Distribution builds append their build host, and a three-part
        release number there would be read as the browser version -- a small
        number that looks like a downgrade against every profile. The leading
        match is the safe end to take, because no product name carries a
        version ahead of its own."""
        assert parse_version(
            "Chromium 151.0.7922.71 built on Ubuntu 22.04.5, running on Ubuntu 22.04.5"
        ) == (151, 0, 7922, 71)

    def test_two_component_suffixes_are_below_the_floor(self):
        """The real Debian shape, and why the floor is three components."""
        assert parse_version(
            "Chromium 90.0.4430.212 built on Debian 10.9, running on Debian 10.9"
        ) == (90, 0, 4430, 212)

    @pytest.mark.parametrize("text", ["", "no version at all", "Chrome 1.0", "148"])
    def test_refuses_to_invent_one(self, text):
        assert parse_version(text) is None


class TestAWrapperThatSaysSomethingFirst:
    """The one input shape where this used to fail *closed*.

    Pointing ``CHROME_PATH`` at a launcher script is the documented way to run
    a browser with extra arguments or under a sandbox, and a script is free to
    announce itself. Reading the whole of stdout, and taking everything before
    the first number as a product name, meant such a banner could donate both
    halves of the comparison. Measured before the fix: a wrapper that printed
    ``Chromium launcher v1.2.3`` and then exec'd Chrome 150 was refused as
    version ``(1, 2, 3)`` of ``Chromium launcher v`` -- older than every
    profile, plausibly enough named to be compared -- and the message told the
    user to run a newer browser, which is what they were doing.
    """

    @pytest.mark.parametrize(
        "stdout",
        [
            # The number is glued to a word, so it is not a version.
            "Chromium launcher v1.2.3\nGoogle Chrome 150.0.7871.187\n",
            # A path that happens to carry one.
            "note: profile at /opt/app-1.2.3\nGoogle Chrome 150.0.7871.187\n",
            # No number at all on the line that answers.
            "Launching Chromium under firejail\nChromium 148.0.7778.96\n",
        ],
    )
    def test_a_banner_is_never_read_as_the_browser(self, stdout):
        assert parse_build(stdout) is None

    @pytest.mark.parametrize(
        "stdout",
        [
            # The shape the whitespace anchor alone does not catch: a properly
            # separated version behind a name that merely begins with one we
            # know. It parses; the product check is what stops it.
            "Chromium launcher 1.2.3\nGoogle Chrome 150.0.7871.187\n",
            "Chrome sandbox helper 2.0.1\nGoogle Chrome 150.0.7871.187\n",
        ],
    )
    def test_a_banner_that_parses_is_still_not_a_product_we_compare(self, stdout):
        build = parse_build(stdout)

        assert build is not None
        assert is_comparable(build.product) is False

    @pytest.mark.parametrize(
        "banner",
        [
            "Chromium launcher v1.2.3",
            "Chromium launcher 1.2.3",
            "Launching Chromium under firejail",
        ],
    )
    def test_a_banner_therefore_only_costs_the_guard(self, tmp_path, banner):
        """Not knowing lets the launch through, which is the whole stance.

        Each of these preceded a real Chrome 150 in the measured case, so a
        refusal here is a refusal of a browser that is *newer* than the
        profile, and the message asks the user to do what they already did.
        """
        profile = _profile_last_opened_by(tmp_path, "150.0.7871.187")
        wrapper = _shell_wrapper(
            tmp_path,
            f"printf '{banner}\\nGoogle Chrome 150.0.7871.187\\n'",
        )

        refuse_a_downgrade(profile, str(wrapper))

    def test_a_trailing_suffix_is_still_read(self):
        """The fix must not cost the real thing it was protecting."""
        assert parse_build("Chromium 151.0.7922.71 snap\n") == BrowserBuild(
            "Chromium", (151, 0, 7922, 71)
        )

    @pytest.mark.parametrize(
        "text, product",
        [
            ("Google Chrome for Testing 148.0.7778.96\n", "Google Chrome for Testing"),
            ("Google Chrome 150.0.7871.189\n", "Google Chrome"),
            ("Brave Browser 151.1.93.129\n", "Brave Browser"),
            ("Chromium 151.0.7922.71 built on Debian 12\n", "Chromium"),
        ],
    )
    def test_the_product_is_whatever_precedes_the_version(self, text, product):
        build = parse_build(text)

        assert build is not None
        assert build.product == product


class TestWhichProductsCompare:
    """``Last Version`` records a number and no product, so the comparison is
    only meaningful inside one numbering scheme.

    Chromium forks number themselves their own way. Vivaldi is on 7.x and Edge
    puts a build number an order of magnitude below Chrome's under the same
    major, so pointing ``CHROME_PATH`` at either against a profile the bundled
    browser wrote reads as a downgrade of hundreds of versions. It is not one,
    and the message it produces asks for something no build of that browser
    will ever satisfy.

    Only the *running* binary can be identified this way, which bounds what the
    rule can do: see ``test_a_foreign_writer_cannot_be_detected``.
    """

    @pytest.mark.parametrize(
        "product",
        [
            "Chromium",
            "Google Chrome",
            "Google Chrome for Testing",
            "google chrome for testing",
        ],
    )
    def test_the_chrome_family_compares(self, product):
        assert is_comparable(product) is True

    def test_an_operators_own_chrome_is_refused_when_it_is_the_older_one(
        self, tmp_path
    ):
        """The only direction in which the `google chrome` entry does any work.

        The obvious case for it -- a profile written by an auto-updating Chrome
        and then opened by the bundled browser -- does not need it: the guard
        reads the *running* binary, never the profile's writer, so the two
        managed names carry that one. What needs it is the reverse, a
        `CHROME_PATH` at a real Chrome that is itself behind the profile.

        Without this test the entry looks removable: every other assertion here
        passes with `google chrome` dropped from the set, and the launch it
        protects would then be waved through with an info-level line nobody
        sees at the default level.
        """
        profile = _profile_last_opened_by(tmp_path, "151.0.7922.76")

        with mock.patch(
            "linkedin_mcp_server.browser_downgrade.version_of",
            return_value=BrowserBuild("Google Chrome", (149, 0, 7827, 55)),
        ):
            with pytest.raises(BrowserDowngradeError) as raised:
                refuse_a_downgrade(profile, "/Applications/Google Chrome.app")

        assert raised.value.browser_product == "Google Chrome"

    @pytest.mark.parametrize(
        "product",
        [
            "Brave Browser",
            "Microsoft Edge",
            "Vivaldi",
            "Opera",
            "Helium",
            "",
            # Matched whole rather than as a prefix, because these are exactly
            # what a launcher script's banner looks like.
            "Chromium launcher",
            "Chrome sandbox helper",
            "Chromium-based Thing",
        ],
    )
    def test_everything_else_does_not(self, product):
        assert is_comparable(product) is False

    @pytest.mark.parametrize(
        "build",
        [
            # Same Chromium major as the profile, build number two thousand
            # lower because Edge numbers its own builds.
            BrowserBuild("Microsoft Edge", (148, 0, 3651, 20)),
            # A fork that never left single digits.
            BrowserBuild("Vivaldi", (7, 5, 3735, 54)),
        ],
    )
    def test_a_foreign_running_browser_is_never_refused(self, tmp_path, build):
        """The whole point. Neither of these is older than a Chrome for Testing
        148 profile in any sense that matters, and the guard cannot tell that
        from the numbers -- so it does not try."""
        profile = _profile_last_opened_by(tmp_path, "148.0.7778.96")

        with mock.patch(
            "linkedin_mcp_server.browser_downgrade.version_of", return_value=build
        ):
            refuse_a_downgrade(profile, "/browser")

    def test_a_foreign_writer_cannot_be_detected(self, tmp_path):
        """The bound on the rule, pinned so nobody later reads more into it.

        A profile written by a fork and then opened by the bundled browser is
        still refused, because ``Last Version`` names no product and the
        running binary is one we do compare. It is not a false refusal that can
        be removed from here: mixing two products over one profile directory is
        outside what this server supports, and the message now says to run
        whichever browser produced that number again -- which is the repair.
        """
        profile = _profile_last_opened_by(tmp_path, "151.1.93.129")

        with mock.patch(
            "linkedin_mcp_server.browser_downgrade.version_of",
            return_value=BrowserBuild("Google Chrome for Testing", (151, 0, 7922, 34)),
        ):
            with pytest.raises(BrowserDowngradeError):
                refuse_a_downgrade(profile, "/browser")


class TestWhatWroteTheProfile:
    def test_absent_directory_says_nothing(self, tmp_path):
        assert profile_was_written_by(tmp_path / "never-existed") is None

    def test_absent_marker_says_nothing(self, tmp_path):
        """The ordinary first run. A profile no browser has opened has no
        marker, and that is not a reason to refuse to open it."""
        fresh = tmp_path / "profile"
        fresh.mkdir()

        assert profile_was_written_by(fresh) is None

    def test_an_unreadable_marker_is_reported(self, tmp_path, caplog):
        """Not silent, unlike an absent one. The file exists, so a browser
        wrote it, and the launch that follows rewrites it down to its own
        number: the guard is gone for this profile from then on."""
        profile = _profile_last_opened_by(tmp_path, "not a version")

        with caplog.at_level(logging.WARNING):
            assert profile_was_written_by(profile) is None

        assert [r for r in caplog.records if r.levelno >= logging.WARNING]

    def test_an_absent_marker_stays_quiet(self, tmp_path, caplog):
        """The ordinary first run is not worth interrupting anyone over."""
        fresh = tmp_path / "fresh"
        fresh.mkdir()

        with caplog.at_level(logging.WARNING):
            assert profile_was_written_by(fresh) is None

        assert not caplog.records

    def test_reads_what_chromium_recorded(self, tmp_path):
        profile = _profile_last_opened_by(tmp_path, "150.0.7871.187")

        assert profile_was_written_by(profile) == (150, 0, 7871, 187)


class TestAskingTheBinary:
    def test_a_binary_that_is_not_there_says_nothing(self, tmp_path):
        """Fails open. A missing browser is the launch's error to report, and
        it says so far better than a guard pretending to know one is there."""
        assert version_of(str(tmp_path / "no-such-browser")) is None

    def test_output_without_a_version_says_nothing(self):
        with mock.patch(
            "linkedin_mcp_server.browser_downgrade.subprocess.run",
            return_value=mock.Mock(stdout="chrome: command not found\n"),
        ):
            assert version_of("/anything") is None

    async def test_the_installed_browser_answers(self):
        """The one assertion here that a Chromium update can break.

        Everything else in this file feeds the parser strings written by hand
        in 2026. If a future build changes what ``--version`` prints, only this
        test notices, and the guard would otherwise fail open in silence for
        every user at once.
        """
        from patchright.async_api import async_playwright

        async with async_playwright() as p:
            executable = p.chromium.executable_path
        if not executable or not Path(executable).exists():
            pytest.skip("chromium is not installed; run patchright install chromium")

        build = version_of(executable)

        assert build is not None, "the installed browser did not report a version"
        assert len(build.version) == 4, f"unexpected version shape: {build}"
        assert build.version[0] > 100, f"implausible major: {build}"
        # And the guard must recognise the product it is about to launch,
        # otherwise it silently stops comparing for every default install.
        assert is_comparable(build.product), f"unrecognised product: {build.product!r}"

    @pytest.mark.skipif(os.name == "nt", reason="needs a POSIX shell wrapper")
    def test_output_it_cannot_decode_still_says_something(self, tmp_path):
        """``text=True`` decodes strictly, and a ``CHROME_PATH`` wrapper is free
        to emit any bytes it likes. A ``UnicodeDecodeError`` is neither an
        ``OSError`` nor a ``SubprocessError``, so without ``errors="replace"``
        it escapes the guard and kills the launch -- the opposite of failing
        open."""
        wrapper = _shell_wrapper(
            tmp_path, "printf 'Chromium 148.0.7778.96 \\377\\376 caf\\351\\n'"
        )

        build = version_of(str(wrapper))

        assert build is not None
        assert build.version == (148, 0, 7778, 96)

    @pytest.mark.skipif(os.name == "nt", reason="needs a POSIX shell wrapper")
    def test_the_probe_never_reads_the_servers_stdin(self, tmp_path):
        """Under the stdio transport that handle is the JSON-RPC channel. A
        wrapper that reads a line would eat an MCP message on its way past and
        then block until the timeout."""
        wrapper = _shell_wrapper(
            tmp_path, "read line\nprintf 'Chromium 148.0.7778.96\\n' \"$line\""
        )
        read_fd, write_fd = os.pipe()
        os.write(write_fd, b"MCP-JSONRPC-LINE-1\nMCP-JSONRPC-LINE-2\n")
        os.close(write_fd)
        saved = os.dup(0)
        try:
            os.dup2(read_fd, 0)
            build = version_of(str(wrapper))
        finally:
            os.dup2(saved, 0)
            os.close(saved)
            leftover = os.read(read_fd, 200)
            os.close(read_fd)

        assert build is not None
        # Both lines are still there: the child read from /dev/null, not from us.
        assert leftover == b"MCP-JSONRPC-LINE-1\nMCP-JSONRPC-LINE-2\n"


class TestWhichWayTheVersionsHaveToGo:
    def test_a_forward_update_is_allowed(self, tmp_path):
        """Chromium migrates a profile it opens. Refusing that would make every
        browser upgrade a manual step, which is not the failure being guarded."""
        profile = _profile_last_opened_by(tmp_path, "148.0.7778.96")

        with mock.patch(
            "linkedin_mcp_server.browser_downgrade.version_of",
            return_value=BrowserBuild("Chromium", (149, 0, 7827, 55)),
        ):
            refuse_a_downgrade(profile, "/browser")

    def test_the_same_version_is_allowed(self, tmp_path):
        profile = _profile_last_opened_by(tmp_path, "148.0.7778.96")

        with mock.patch(
            "linkedin_mcp_server.browser_downgrade.version_of",
            return_value=BrowserBuild("Chromium", (148, 0, 7778, 96)),
        ):
            refuse_a_downgrade(profile, "/browser")

    def test_an_older_browser_is_refused(self, tmp_path):
        profile = _profile_last_opened_by(tmp_path, "150.0.7871.187")

        with mock.patch(
            "linkedin_mcp_server.browser_downgrade.version_of",
            return_value=BrowserBuild("Chromium", (148, 0, 7778, 96)),
        ):
            with pytest.raises(BrowserDowngradeError) as raised:
                refuse_a_downgrade(profile, "/browser")

        # Both numbers, because neither alone lets anyone act: the message has
        # to say what to point CHROME_PATH at.
        assert raised.value.profile_version == "150.0.7871.187"
        assert raised.value.browser_version == "148.0.7778.96"
        assert "150.0.7871.187" in str(raised.value)
        assert "148.0.7778.96" in str(raised.value)

    def test_a_point_release_backwards_is_refused(self, tmp_path):
        """The whole version, not the major.

        Chrome's own downgrade detection compares the full version, and the
        schema floors that make a downgrade lossy are raised on point releases
        as readily as on majors. A major-only comparison would wave through the
        commonest shape of this: two installs of the same series, one stale.
        """
        profile = _profile_last_opened_by(tmp_path, "148.0.7778.96")

        with mock.patch(
            "linkedin_mcp_server.browser_downgrade.version_of",
            return_value=BrowserBuild("Chromium", (148, 0, 7778, 95)),
        ):
            with pytest.raises(BrowserDowngradeError):
                refuse_a_downgrade(profile, "/browser")


class TestThePlatformWhereAskingWouldStartABrowser:
    """Windows, where ``--version`` is not answered but ignored.

    Chromium compiles ``HandleVersionSwitches`` and its call site only under
    ``BUILDFLAG(IS_POSIX)`` (``chrome/app/chrome_main_delegate.cc``, checked
    against current main), so on Windows the switch is unrecognised and an
    unrecognised switch does not stop a browser starting. With no
    ``--user-data-dir`` it opens whatever profile that binary defaults to, which
    with ``CHROME_PATH`` is the user's own Chrome: either a window appears and
    the process forwards its command line, or the probe *is* the browser, blocks
    for the whole timeout, and is then killed, which is what makes Chrome offer
    to restore pages afterwards.

    The Windows CI job runs four unrelated files, so nothing here would have
    seen it. These tests force the platform answer instead of relying on it.
    """

    def test_no_binary_is_ever_run_there(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "linkedin_mcp_server.browser_downgrade.a_version_can_be_asked_for",
            lambda: False,
        )
        with mock.patch(
            "linkedin_mcp_server.browser_downgrade.subprocess.run"
        ) as spawned:
            assert version_of("/anything") is None

        assert spawned.call_count == 0

    def test_the_guard_gives_up_before_it_reads_anything(self, tmp_path, monkeypatch):
        """Ahead of the marker, so the platform is silent rather than warning
        on every start about a check it can never perform."""
        profile = _profile_last_opened_by(tmp_path, "150.0.7871.187")
        monkeypatch.setattr(
            "linkedin_mcp_server.browser_downgrade.a_version_can_be_asked_for",
            lambda: False,
        )

        with mock.patch("linkedin_mcp_server.browser_downgrade.version_of") as asked:
            refuse_a_downgrade(profile, "/browser")

        assert asked.call_count == 0

    def test_it_says_nothing_at_the_default_level(self, tmp_path, monkeypatch, caplog):
        """An *unparseable* marker, which is the only fixture that can see the
        ordering. With a good one the marker read is silent either way, so the
        mutation that moves it above the platform gate passes unnoticed; with
        this one that mutation produces a warning on every Windows start about
        a check the platform can never perform."""
        profile = _profile_last_opened_by(tmp_path, "not a version")
        monkeypatch.setattr(
            "linkedin_mcp_server.browser_downgrade.a_version_can_be_asked_for",
            lambda: False,
        )

        with caplog.at_level(logging.WARNING):
            refuse_a_downgrade(profile, "/browser")

        assert not caplog.records, [r.getMessage() for r in caplog.records]

    def test_posix_still_asks(self, monkeypatch):
        """The other half, so the platform switch cannot be left stuck off."""
        monkeypatch.setattr(os, "name", "posix")

        assert a_version_can_be_asked_for() is True

        monkeypatch.setattr(os, "name", "nt")

        assert a_version_can_be_asked_for() is False


class TestWhatTheUserIsToldWhenItGivesUp:
    """The default log level is WARNING, so anything quieter is invisible."""

    def test_an_unnameable_binary_is_reported(self, tmp_path, caplog):
        """The one branch where half the evidence exists and is about to go.

        The marker was read, the launch proceeds, and the older browser
        rewrites that marker down to its own number on the way out. The guard
        can never fire on this profile again, so without a line here the
        eventual "session expired" has nothing to connect it to.
        """
        profile = _profile_last_opened_by(tmp_path, "150.0.7871.187")

        with caplog.at_level(logging.WARNING):
            with mock.patch(
                "linkedin_mcp_server.browser_downgrade.version_of", return_value=None
            ):
                refuse_a_downgrade(profile, "/browser")

        assert any(
            record.levelno >= logging.WARNING
            and "150.0.7871.187" in record.getMessage()
            for record in caplog.records
        ), f"nothing at WARNING: {[r.getMessage() for r in caplog.records]}"

    def test_a_foreign_product_stays_below_a_warning(self, tmp_path, caplog):
        """That one follows directly from an explicit CHROME_PATH outside the
        Chrome family, so it is the configuration behaving as documented rather
        than a surprise worth interrupting anyone over."""
        profile = _profile_last_opened_by(tmp_path, "150.0.7871.187")

        with caplog.at_level(logging.DEBUG):
            with mock.patch(
                "linkedin_mcp_server.browser_downgrade.version_of",
                return_value=BrowserBuild("Vivaldi", (7, 5, 3735, 54)),
            ):
                refuse_a_downgrade(profile, "/browser")

        assert [r for r in caplog.records if r.levelno == logging.INFO]
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


class TestWhereItFailsOpen:
    """Not knowing is never evidence of a downgrade.

    Every unknown here has a live counterpart: Windows prints no version at
    all, a custom ``CHROME_PATH`` can name a wrapper script, and a profile
    directory can be on a filesystem that refuses the read.
    """

    def test_an_unnameable_binary_is_allowed(self, tmp_path):
        profile = _profile_last_opened_by(tmp_path, "150.0.7871.187")

        with mock.patch("linkedin_mcp_server.browser_downgrade.version_of") as asked:
            refuse_a_downgrade(profile, None)

        assert asked.call_count == 0

    def test_a_silent_binary_is_allowed(self, tmp_path):
        profile = _profile_last_opened_by(tmp_path, "150.0.7871.187")

        with mock.patch(
            "linkedin_mcp_server.browser_downgrade.version_of", return_value=None
        ):
            refuse_a_downgrade(profile, "/browser")

    def test_a_fresh_profile_is_not_worth_a_subprocess(self, tmp_path):
        """Order, not just outcome.

        With nothing recorded there is nothing to compare against, so asking
        the binary costs a process spawn to learn nothing. Reversing the two
        still passes every other test here, which is why this one asserts that
        the marker is read first.
        """
        fresh = tmp_path / "profile"
        fresh.mkdir()

        with mock.patch("linkedin_mcp_server.browser_downgrade.version_of") as asked:
            refuse_a_downgrade(fresh, "/browser")

        assert asked.call_count == 0


def _fake_playwright(
    recorder: dict,
    *,
    executable: str = "/registry/chromium",
    stop_hangs: bool = False,
):
    """A driver that records the launch it was asked for and nothing else.

    Deliberately smaller than the fake in ``test_core_browser``: the hidden
    target is out of the way here, because what is under test is whether the
    launch happens at all.
    """

    class _Page:
        url = "about:blank"

        async def close(self):
            return None

    class _Context:
        def __init__(self):
            self.pages = [_Page()]

        async def close(self):
            return None

    class _Chromium:
        executable_path = executable

        async def launch_persistent_context(self, user_data_dir, **kwargs):
            recorder["options"] = kwargs
            recorder["user_data_dir"] = user_data_dir
            return _Context()

    class _Playwright:
        chromium = _Chromium()

        async def stop(self):
            if stop_hangs:
                await asyncio.sleep(30)
            return None

    async def start():
        return _Playwright()

    return start


class TestWhatTheLaunchDoes:
    """The guard where it actually sits, not the helper on its own."""

    def _manager(self, profile: Path, **kwargs) -> BrowserManager:
        return BrowserManager(user_data_dir=profile, headless=True, **kwargs)

    async def _start_against(
        self, manager: BrowserManager, recorder: dict, **fake_kwargs
    ):
        with mock.patch(
            "linkedin_mcp_server.core.browser.hidden_target_is_supported",
            return_value=False,
        ):
            with mock.patch(
                "linkedin_mcp_server.core.browser.async_playwright"
            ) as playwright:
                playwright.return_value.start = _fake_playwright(
                    recorder, **fake_kwargs
                )
                await manager.start()

    async def test_the_profile_is_never_opened(self, tmp_path):
        """A refusal that still launched would have migrated the profile before
        anyone read the message, which is the damage being avoided."""
        profile = _profile_last_opened_by(tmp_path, "150.0.7871.187")
        recorder: dict = {}
        manager = self._manager(profile, executable_path="/browser")

        with mock.patch(
            "linkedin_mcp_server.browser_downgrade.version_of",
            return_value=BrowserBuild("Chromium", (148, 0, 7778, 96)),
        ):
            with pytest.raises(BrowserDowngradeError):
                await self._start_against(manager, recorder)

        assert "options" not in recorder, "the browser was launched anyway"

    async def test_nothing_touches_the_profile_before_the_refusal(self, tmp_path):
        """Ordering, and the only part of it that is observable.

        A refusal cannot be reached on a directory that does not exist -- there
        would be no marker to read -- so "the directory was not created" proves
        nothing. What the position of the check does decide is whether the
        profile is created and its whole tree chmodded on the way to being
        refused, and moving the check below either of those fails here.
        """
        profile = _profile_last_opened_by(tmp_path, "150.0.7871.187")
        recorder: dict = {}
        manager = self._manager(profile, executable_path="/browser")

        with (
            mock.patch("linkedin_mcp_server.core.browser.secure_mkdir") as made,
            mock.patch(
                "linkedin_mcp_server.core.browser.harden_linkedin_tree"
            ) as hardened,
            mock.patch(
                "linkedin_mcp_server.browser_downgrade.version_of",
                return_value=BrowserBuild("Chromium", (148, 0, 7778, 96)),
            ),
        ):
            with pytest.raises(BrowserDowngradeError):
                await self._start_against(manager, recorder)

        assert made.call_count == 0
        assert hardened.call_count == 0

    async def test_it_is_not_reported_as_a_network_failure(self, tmp_path):
        """``start()`` wraps everything it catches into ``NetworkError``, and
        that class is read downstream as a missing binary: the recovery
        invalidates the browser metadata and reinstalls the same old revision,
        which cannot help. It has to pass through as itself.

        The ``pytest.raises`` is the assertion. Under the mutation that drops
        the passthrough, what arrives is a ``NetworkError`` and this fails.
        Asserting ``not isinstance(..., NetworkError)`` afterwards would prove
        nothing at all, because the class cannot inherit it.
        """
        profile = _profile_last_opened_by(tmp_path, "150.0.7871.187")
        recorder: dict = {}
        manager = self._manager(profile, executable_path="/browser")

        with mock.patch(
            "linkedin_mcp_server.browser_downgrade.version_of",
            return_value=BrowserBuild("Chromium", (148, 0, 7778, 96)),
        ):
            with pytest.raises(BrowserDowngradeError) as raised:
                await self._start_against(manager, recorder)

        # The message survives the trip too: the wrapping replaces it with
        # "Failed to start browser: ...", which names neither version.
        assert "150.0.7871.187" in str(raised.value)
        assert "148.0.7778.96" in str(raised.value)

    async def test_a_wedged_driver_does_not_hide_the_reason(self, tmp_path):
        """No Chromium ever ran, so nothing can be holding the profile.

        The refusal happens before ``launch_persistent_context``; only the
        driver process is up. A driver that misses its stop bound would
        otherwise replace the actionable message with the generic
        shutdown-unconfirmed one, and the caller marks the profile busy for the
        rest of the process's life on the strength of that -- over a profile
        that was never opened.
        """
        profile = _profile_last_opened_by(tmp_path, "150.0.7871.187")
        recorder: dict = {}
        manager = self._manager(profile, executable_path="/browser")

        with (
            mock.patch(
                "linkedin_mcp_server.core.browser._CLEANUP_TIMEOUT_SECONDS", 0.05
            ),
            mock.patch(
                "linkedin_mcp_server.browser_downgrade.version_of",
                return_value=BrowserBuild("Chromium", (148, 0, 7778, 96)),
            ),
        ):
            with pytest.raises(BrowserDowngradeError):
                await self._start_against(manager, recorder, stop_hangs=True)

    async def test_a_matching_browser_launches_normally(self, tmp_path):
        """The other half of the contract: the guard must not stop a launch it
        has no reason to stop."""
        profile = _profile_last_opened_by(tmp_path, "148.0.7778.96")
        recorder: dict = {}
        manager = self._manager(profile, executable_path="/browser")

        with mock.patch(
            "linkedin_mcp_server.browser_downgrade.version_of",
            return_value=BrowserBuild("Chromium", (148, 0, 7778, 96)),
        ):
            await self._start_against(manager, recorder)

        assert "options" in recorder

    async def test_a_managed_launch_checks_the_registry_binary(self, tmp_path):
        """No ``CHROME_PATH``, so the binary comes from Playwright's registry --
        and that is the one nearly every user runs. Asking only when a custom
        path was configured would leave the default install unguarded."""
        profile = _profile_last_opened_by(tmp_path, "150.0.7871.187")
        recorder: dict = {}
        manager = self._manager(profile)

        with mock.patch(
            "linkedin_mcp_server.browser_downgrade.version_of",
            return_value=BrowserBuild("Chromium", (148, 0, 7778, 96)),
        ) as asked:
            with pytest.raises(BrowserDowngradeError):
                await self._start_against(manager, recorder)

        assert asked.call_args.args == ("/registry/chromium",)


class TestWhichBinaryIsChecked:
    def test_an_explicit_path_wins(self, tmp_path):
        """``CHROME_PATH`` is the operator's own choice and is what Playwright
        launches, so it is what gets asked."""
        manager = BrowserManager(
            user_data_dir=tmp_path, executable_path="/opt/chrome/chrome"
        )

        assert manager._executable_about_to_run() == "/opt/chrome/chrome"

    def test_the_registry_answers_for_a_managed_launch(self, tmp_path):
        manager = BrowserManager(user_data_dir=tmp_path)
        manager._playwright = mock.Mock()
        manager._playwright.chromium.executable_path = "/cache/chromium/chrome"

        assert manager._executable_about_to_run() == "/cache/chromium/chrome"

    def test_no_driver_yet_means_no_answer(self, tmp_path):
        manager = BrowserManager(user_data_dir=tmp_path)

        assert manager._executable_about_to_run() is None

    def test_an_empty_registry_answer_is_not_a_name(self, tmp_path):
        """Playwright's registry returns ``... || ""`` when nothing is
        installed. Passed on, that buys a doomed ``subprocess.run(["", ...])``
        and then a warning naming no binary at all; the launch that follows
        reports the missing browser properly."""
        manager = BrowserManager(user_data_dir=tmp_path)
        manager._playwright = mock.Mock()
        manager._playwright.chromium.executable_path = ""

        assert manager._executable_about_to_run() is None

    def test_a_registry_that_refuses_to_answer_is_not_fatal(self, tmp_path):
        """Playwright's ``executable_path`` raises when the initializer is
        empty, which the guard must survive: not being able to name the binary
        says nothing about whether it is older than the profile."""
        manager = BrowserManager(user_data_dir=tmp_path)
        manager._playwright = mock.Mock()
        type(manager._playwright.chromium).executable_path = mock.PropertyMock(
            side_effect=RuntimeError("Please install the browser first")
        )

        assert manager._executable_about_to_run() is None


class TestWhereARefusalIsRecoverableOnItsOwn:
    """One profile is the session; the other is a cache of it.

    The source profile has to reach the user, because throwing it away to
    satisfy an old browser is the damage rather than the repair. A derived
    runtime profile is rebuilt from the source cookies on demand, and the
    re-bridge deletes it first, so the older browser gets a directory it wrote
    itself. Reachable whenever a container image moves backwards with
    ``EXPERIMENTAL_PERSIST_DERIVED_RUNTIME`` set.
    """

    def _committed_derived_runtime(self, monkeypatch, tmp_path):
        """Enough state for ``_create_browser_locked`` to choose the stored
        derived profile: a source session, a matching generation, and both the
        profile and its storage snapshot on disk."""
        from linkedin_mcp_server import session_state
        from linkedin_mcp_server.drivers import browser as drv

        source = tmp_path / "source" / "profile"
        source.mkdir(parents=True)
        (source / "Cookies").write_text("source")
        session_state.portable_cookie_path(source).write_text("[]")
        derived = session_state.runtime_profile_dir("linux-amd64-container", source)
        derived.mkdir(parents=True)
        (derived / "Cookies").write_text("derived")
        snapshot = session_state.runtime_storage_state_path(
            "linux-amd64-container", source
        )
        snapshot.write_text("{}")

        source_state = session_state.SourceState(
            version=1,
            source_runtime_id="macos-arm64-host",
            login_generation="gen-1",
            created_at="2026-08-06T09:00:00Z",
            profile_path=str(source),
            cookies_path=str(session_state.portable_cookie_path(source)),
        )
        runtime_state = session_state.RuntimeState(
            version=1,
            runtime_id="linux-amd64-container",
            source_runtime_id="macos-arm64-host",
            source_login_generation="gen-1",
            created_at="2026-08-06T09:00:00Z",
            committed_at="2026-08-06T09:00:05Z",
            profile_path=str(derived),
            storage_state_path=str(snapshot),
            commit_method="checkpoint_restart",
        )

        monkeypatch.setattr(drv, "_launch_options", lambda: ({}, {}))
        monkeypatch.setattr(drv, "get_profile_dir", lambda: source)
        monkeypatch.setattr(drv, "load_source_state", lambda _dir: source_state)
        monkeypatch.setattr(drv, "get_runtime_id", lambda: "linux-amd64-container")
        monkeypatch.setattr(drv, "experimental_persist_derived_runtime", lambda: True)
        monkeypatch.setattr(drv, "_debug_bridge_every_startup", lambda: False)
        monkeypatch.setattr(drv, "load_runtime_state", lambda *_a: runtime_state)
        monkeypatch.setattr(drv, "_apply_browser_settings", lambda _b: None)
        return drv, derived

    async def test_a_stale_derived_profile_is_rebridged(self, monkeypatch, tmp_path):
        drv, derived = self._committed_derived_runtime(monkeypatch, tmp_path)
        bridged = mock.Mock(name="bridged browser")

        async def refuse(profile_dir, **_kwargs):
            raise BrowserDowngradeError(
                profile_version="151.0.7922.34",
                browser_version="149.0.7827.55",
                profile_dir=profile_dir,
            )

        async def bridge(profile_dir, **_kwargs):
            return bridged

        monkeypatch.setattr(drv, "_authenticate_existing_profile", refuse)
        monkeypatch.setattr(drv, "_bridge_runtime_profile", bridge)

        assert await drv._create_browser_locked() is bridged

    async def test_the_rebridge_really_clears_the_marker_first(
        self, monkeypatch, tmp_path
    ):
        """The claim the re-bridge rests on, exercised rather than asserted.

        Catching a downgrade only helps because ``_bridge_runtime_profile``
        deletes the derived profile before it launches, so the older browser
        meets a directory with no ``Last Version`` in it. The test above
        substitutes that function, so nothing there would notice a refactor
        that moved the clear below the launch -- the caught downgrade would
        simply be raised again, uncaught, and every test would still pass.
        """
        from linkedin_mcp_server import session_state
        from linkedin_mcp_server.drivers import browser as drv
        from linkedin_mcp_server.profile_claim import ensure_profile_claim

        source = tmp_path / "source" / "profile"
        source.mkdir(parents=True)
        # The delete below goes through the ownership guard, which refuses a
        # root nobody claimed. Claiming it is part of setting the scene, not
        # part of what is under test.
        ensure_profile_claim(source, claim_anyway=True)
        derived = session_state.runtime_profile_dir("linux-amd64-container", source)
        derived.mkdir(parents=True)
        (derived / "Last Version").write_text("151.0.7922.34")

        seen: dict = {}

        def _look(profile_dir, **_kwargs):
            seen["marker_at_launch"] = (profile_dir / "Last Version").exists()
            raise RuntimeError("far enough")

        monkeypatch.setattr(drv, "get_source_profile_dir", lambda: source)
        monkeypatch.setattr(drv, "_make_browser", _look)

        with pytest.raises(RuntimeError, match="far enough"):
            await drv._bridge_runtime_profile(
                derived,
                cookie_path=session_state.portable_cookie_path(source),
                source_state=mock.Mock(login_generation="gen-1"),
                runtime_id="linux-amd64-container",
                launch_options={},
                viewport={},
                persist_runtime=True,
            )

        assert seen["marker_at_launch"] is False

    async def test_the_source_profile_still_refuses(self, monkeypatch, tmp_path):
        """Same failure, opposite answer, because the source profile is the
        session itself. Nothing downstream may quietly rebuild it."""
        from linkedin_mcp_server import session_state
        from linkedin_mcp_server.drivers import browser as drv

        source = tmp_path / "source" / "profile"
        source.mkdir(parents=True)
        (source / "Cookies").write_text("source")
        session_state.portable_cookie_path(source).write_text("[]")
        source_state = session_state.SourceState(
            version=1,
            source_runtime_id="macos-arm64-host",
            login_generation="gen-1",
            created_at="2026-08-06T09:00:00Z",
            profile_path=str(source),
            cookies_path=str(session_state.portable_cookie_path(source)),
        )

        async def refuse(profile_dir, **_kwargs):
            raise BrowserDowngradeError(
                profile_version="151.0.7922.34",
                browser_version="149.0.7827.55",
                profile_dir=profile_dir,
            )

        monkeypatch.setattr(drv, "_launch_options", lambda: ({}, {}))
        monkeypatch.setattr(drv, "get_profile_dir", lambda: source)
        monkeypatch.setattr(drv, "load_source_state", lambda _dir: source_state)
        monkeypatch.setattr(drv, "get_runtime_id", lambda: "macos-arm64-host")
        monkeypatch.setattr(drv, "_authenticate_existing_profile", refuse)

        # The `raises` is the whole assertion. Checking the profile survived
        # afterwards would prove nothing here, because the stand-in for
        # `_authenticate_existing_profile` only raises and could not have
        # touched it under any mutation.
        with pytest.raises(BrowserDowngradeError):
            await drv._create_browser_locked()
