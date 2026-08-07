"""Refuse to open a profile with a browser older than the one that wrote it.

Chromium records its own version in ``<user-data-dir>/Last Version`` on every
run and treats a later launch by an older binary as a downgrade
(``downgrade_manager.cc``). It does not stop there on macOS or Linux -- it
carries on and lets each store decide for itself. That is the failure mode this
module exists to prevent: a store whose *compatible* version has been raised
past what the older binary accepts is rejected with ``INIT_TOO_NEW`` and the
browser simply runs without it. Cookies are such a store, so the visible symptom
is a session that was never invalid disappearing, followed by a login nobody
asked for.

Measured, and worth keeping because it explains why the check is stricter than
Chromium's own: bundled Chromium 148 opened a profile last written by Chrome 150
with cookies, Local Storage, IndexedDB and Cache Storage all intact, because
Chrome 150 marks its Web Data schema as compatible back that far. It also
*rewrote* the version markers on the way out. So the compatibility is real, and
it is a snapshot -- it holds until one store raises its floor, and nothing warns
when that happens.

Every answer here fails open. Being unable to read a marker, name the binary or
parse a version is not evidence of a downgrade, and refusing to start a browser
on the strength of a missing file would turn a guard into an outage.

That choice costs more than it looks like it does, and the cost belongs here
rather than in a reviewer's head. Failing open once is not "we will catch it
next time": the older browser runs, and on its way out it rewrites `Last
Version` down to its own number (measured -- a launch on a profile marked
`1.0.0.0` left it reading `148.0.7778.96`). The evidence is gone, so every later
launch sees no downgrade even after whatever made the version unreadable is
fixed. It is still the right trade, because the alternative refuses working
setups on the strength of not knowing, but the guard is one-shot per profile
and nothing downstream can recover it.
"""

import logging
import os
import re
import subprocess
from pathlib import Path
from typing import NamedTuple

from linkedin_mcp_server.exceptions import BrowserDowngradeError

logger = logging.getLogger(__name__)

#: Written by Chromium itself, directly inside the profile directory, with a
#: space in the name and no extension. Not one of ours, so it is never created
#: or removed here -- only read.
LAST_VERSION_FILE = "Last Version"

#: A binary that will not answer ``--version`` must not hold up the launch.
#: Generous rather than tight: the measured cost is ~35 ms warm, and the only
#: thing a low bound would buy is a false "unknown" on a loaded machine.
_VERSION_TIMEOUT_SECONDS = 15.0

#: Three components at minimum, so a two-part build suffix ("built on Debian
#: 10.9") cannot be mistaken for a version. Real values have four:
#: ``148.0.7778.96``.
#:
#: Anchored to whitespace or the start of the line, which is the part that stops
#: the guard failing *closed*. A `CHROME_PATH` launcher script announcing itself
#: as `Chromium launcher v1.2.3` before it execs a browser would otherwise be
#: read as version `1.2.3` of a product called `Chromium launcher v` -- older
#: than every profile, and named plausibly enough to be compared. The user is
#: then told to run a newer browser, which is exactly what they were doing.
#: A version glued to a preceding word is not a version, so it is not one here.
_VERSION = re.compile(r"(?:^|(?<=\s))\d+(?:\.\d+){2,}")

#: Product names whose version numbers track Chromium milestones, and which are
#: therefore comparable against a `Last Version` that carries no product of its
#: own.
#:
#: Matched whole, not as a prefix, and that is the second half of the wrapper
#: defence rather than a stylistic choice. A `CHROME_PATH` launcher announcing
#: itself as `Chromium launcher 1.2.3` parses cleanly -- the version is
#: space-separated and the name starts with a word we know -- so a prefix scan
#: accepted it, compared `1.2.3` against the profile, and refused a browser
#: that was in fact newer. Measured. A name we have not seen costs the guard;
#: a name we half-recognise cost a launch nobody could recover.
#:
#: Chromium forks number themselves their own way: Vivaldi is on 7.x, and Edge
#: puts a build number an order of magnitude below Chrome's under the same
#: major. Pointing `CHROME_PATH` at either, against a profile the bundled
#: browser wrote, reads as a downgrade of hundreds of versions. It is not one,
#: and the message it would produce asks for something no build of that
#: browser will ever satisfy.
#:
#: This identifies the *running* binary, which is all `--version` can tell us,
#: and that bounds the rule: a profile written by a fork and then opened by the
#: bundled browser is still refused, because `Last Version` names no product.
#: That case is not repairable from here. Mixing two products over one profile
#: directory is outside what this server supports, and the error says to run
#: whichever browser produced that number again, which is the way out.
#:
#: An unrecognised name costs the guard, never a false refusal, which is the
#: right direction to be wrong in.
#:
#: **Two of these entries are needed at the same time, on the same release, and
#: the axis is the platform rather than the version.** Playwright downloads
#: Chrome for Testing for most targets but its own Chromium build for Linux
#: arm64, and the two binaries name themselves differently. From the driver's
#: own `DOWNLOAD_PATHS` at the current lock:
#:
#: * every supported `ubuntu*-arm64` and `debian*-arm64` ->
#:   `chromium-linux-arm64.zip`, which unpacks to `chrome-linux/` and reports
#:   `Chromium`
#: * Linux x64, every macOS, and `win64` -> `cftUrl(...)`, which unpacks to
#:   `chrome-linux64/` or `Google Chrome for Testing.app` and reports `Google
#:   Chrome for Testing`
#:
#: (`ubuntu18.04-*` is `void 0` in that table on both branches: patchright will
#: not install there at all, so it names nothing.)
#:
#: Both container architectures are published, so both names are in the field
#: on any given release. Dropping either entry as redundant would silently turn
#: the guard off for a shipped platform, which is the mistake this paragraph
#: exists to prevent.
#:
#: There is a version axis underneath as well, and it is the smaller one.
#: Revision 1200 (patchright 1.57.0) moved macOS *and* Linux x64 to Chrome for
#: Testing together; only Linux arm64 stayed on Playwright's own build, and
#: that is the split that survives to the lock. Read it off `EXECUTABLE_PATHS`
#: rather than the download table: at 1.57.0 `linux-x64` is
#: `["chrome-linux64", "chrome"]` while `linux-arm64` is still
#: `["chrome-linux", "chrome"]`, carrying the driver's own `// non-cft build`
#: comment.
#:
#: Two separate events are easy to conflate here, and conflating them is how
#: the first version of this paragraph got the story wrong: *what* is packaged
#: changed at revision 1200, while *where it is fetched from* changed at
#: patchright 1.58.0, when `cftUrl(...)` first appears. That second move is not
#: a move to Google: `cftUrl` points at Playwright's own CDN in both eras,
#: `cdn.playwright.dev/chrome-for-testing-public/<version>/...` at 1.58.0 and
#: `cdn.playwright.dev/builds/cft/<version>/...` at the lock. Nothing here ever
#: fetches from `storage.googleapis.com`.
#:
#: For anyone allowlisting egress, one host is not the whole answer. Every
#: plain `builds/...` entry goes through `PLAYWRIGHT_CDN_MIRRORS`, three hosts
#: tried in order: `cdn.playwright.dev/dbazure/download/playwright`,
#: `playwright.download.prss.microsoft.com/dbazure/download/playwright`, then
#: `cdn.playwright.dev`. That covers the arm64 image's browser, and ffmpeg on
#: *every* platform, since `patchright install chromium` resolves that too and
#: its entries are plain templates. Where `cftUrl` entries exist they are the
#: other shape:
#: one host, `cdn.playwright.dev`, and no fallback at all. Allowing only
#: `cdn.playwright.dev` therefore installs everything, because two of the three
#: mirrors are on it; what is lost is the Microsoft-hosted fallback, so the gap
#: shows up only when the primary CDN is degraded.
#:
#: At the floor every target still reads from `builds/chromium/`, which says
#: nothing about which browser is inside. Binaries run directly: revision
#: 1194 ->
#: `Chromium 141.0.7390.37`, revision 1200 -> `Google Chrome for Testing
#: 143.0.7499.4`, and revisions 1217, 1223 and 1228 -> `Google Chrome for
#: Testing`, all on macOS arm64. On Linux arm64 the container reported
#: `Chromium 148.0.7778.0` at revision 1223 -- the same revision that reports
#: `Google Chrome for Testing 148.0.7778.96` on macOS. One revision, two names,
#: decided by the platform: that pair is the clearest statement of the rule.
#:
#: The other platforms, measured under Docker rather than reasoned about, since
#: reasoning is what got this paragraph wrong the first time. At the floor
#: (revision 1187) linux-x64 and linux-arm64 both report `Chromium
#: 140.0.7339.16`, exactly as macOS does -- so at the floor there is no
#: platform split at all. At the lock (revision 1228) linux-x64 reports `Google
#: Chrome for Testing 149.0.7827.55` while linux-arm64 reports `Chromium
#: 149.0.7827.0`, note the differing final component. Windows never answers:
#: :func:`a_version_can_be_asked_for` refuses before any binary is asked.
#:
#: The third entry is not a managed browser at all. `Google Chrome
#: 150.0.7871.189` is what an operator's own binary reports when `CHROME_PATH`
#: points at real Chrome.
#:
#: Note carefully which direction needs it, because the obvious one does not.
#: :func:`is_comparable` is only ever asked about the *running* binary, never
#: about whatever wrote the profile -- `Last Version` records no product to ask
#: about. So the familiar case, a Chrome-written profile opened by the bundled
#: browser, is carried by the two managed names and would still be refused with
#: this entry gone. What needs it is the reverse: `CHROME_PATH` at a real
#: Chrome that is itself the older binary, against a profile a newer Chrome or
#: the bundled Chrome for Testing already wrote. Drop the entry and that launch
#: stops being checked at all, silently.
#:
#: Do not infer any of this from `browsers.json`. Its `title` key did not exist
#: before patchright 1.58.0, and where it does exist it reads `Chrome for
#: Testing` without the `Google` the binary prints. It says which family a
#: build belongs to and nothing about the string matched here.
#:
#: Distribution and snap builds put the build host after the number, and the
#: beta and dev channels put the channel there, so all of those still read as
#: `Chromium` or `Google Chrome`.
#:
#: One entry nobody here could measure: Chrome Canary. If it names itself
#: `Google Chrome Canary` rather than putting the channel after the version,
#: the guard is simply off for that install, which is the direction this errs
#: in anyway. Recorded rather than guessed at.
_COMPARABLE_PRODUCTS = frozenset(
    {"chromium", "google chrome", "google chrome for testing"}
)


class BrowserBuild(NamedTuple):
    """What a binary says it is: the product name and the version beside it."""

    product: str
    version: tuple[int, ...]


def a_version_can_be_asked_for() -> bool:
    """Whether asking a browser binary for its version is a safe thing to do.

    False on Windows, and not because the answer would merely be empty.
    Chromium handles ``--version`` in ``HandleVersionSwitches``, and both that
    function and its call site in ``BasicStartupComplete`` sit inside
    ``#if BUILDFLAG(IS_POSIX) && !defined(BUILDING_CHROME_RENDERER)``
    (``chrome/app/chrome_main_delegate.cc``, verified against current main). On
    Windows that code is not compiled, so ``--version`` is not a handled switch
    but an *unrecognised* one, and an unrecognised switch does not stop a
    browser from starting.

    What the probe would actually do there: no ``--user-data-dir`` is passed,
    so it opens whatever profile that binary defaults to, which with
    ``CHROME_PATH`` set is the user's own Chrome. If a browser is already
    running on it, the new process forwards its command line and exits, leaving
    a window open and no output. If none is, the probe *is* the browser: it
    never exits, blocks the launch for the full timeout, and is then killed,
    which is what makes Chrome offer to restore pages on the user's next start.

    So the guard is off on Windows, deliberately and quietly. There is no half
    measure available: a version could only come from the executable's own
    file-version resource, which is separate work.
    """
    return os.name != "nt"


def _the_line_that_should_carry_it(text: str) -> str:
    """The first non-empty line of *text*, which is where a browser answers.

    Everything after it is out of scope on purpose. A wrapper script that says
    something before handing over would otherwise donate both the number and
    the product name to the parse, and the guard would compare a banner. Kept
    to one line, a banner simply means no version is found, and not knowing
    lets the launch proceed -- the direction this module is meant to fail in.
    """
    for line in text.splitlines():
        if line.strip():
            return line
    return ""


def parse_version(text: str) -> tuple[int, ...] | None:
    """The first dotted version on *text*'s first line, as a comparable tuple.

    The first rather than the last, and the difference is load-bearing on
    Linux. Distribution builds append their own build host: `Chromium
    90.0.4430.212 built on Debian 10.9, running on Debian 10.9` is harmless
    because two components do not match, but a three-part release number in
    that suffix -- an Ubuntu point release, say -- would be picked up as the
    browser version and read as a downgrade against every profile. No real
    product name carries a version ahead of its own, so the leading match is
    the safe end to take.

    The same parser reads the ``Last Version`` file, which holds the bare
    number on one line, where first and last are the same thing.
    """
    build = parse_build(text)
    return None if build is None else build.version


def parse_build(text: str) -> BrowserBuild | None:
    """A ``--version`` line split into the product name and the version."""
    line = _the_line_that_should_carry_it(text)
    match = _VERSION.search(line)
    if match is None:
        return None
    return BrowserBuild(
        product=line[: match.start()].strip(),
        version=tuple(int(part) for part in match.group().split(".")),
    )


def is_comparable(product: str) -> bool:
    """Whether *product* numbers itself the way ``Last Version`` is numbered.

    Whole name, not a prefix. Every published Chrome-family build puts its
    channel or build host *after* the version (`Chromium 151.0.7922.71 snap`,
    `Google Chrome 150.0.7871.189 beta`), so the token ahead of the number is
    always one of these three and nothing is lost by being strict.
    """
    return product.strip().lower() in _COMPARABLE_PRODUCTS


def profile_was_written_by(profile_dir: Path) -> tuple[int, ...] | None:
    """The version Chromium last recorded in *profile_dir*, if it said."""
    marker = profile_dir / LAST_VERSION_FILE
    try:
        recorded = marker.read_text(encoding="utf-8", errors="replace")
    except OSError:
        # Absent on a profile no browser has opened yet, which is the ordinary
        # first run and not worth a log line above debug.
        return None
    version = parse_version(recorded)
    if version is None:
        # A warning, not a debug line, and for the same reason the unnameable
        # binary below gets one: the file exists, so a browser wrote it, and
        # the launch that follows will rewrite it down to its own number. That
        # is the guard gone for this profile permanently, and the default log
        # level is WARNING, so anything quieter is invisible where it counts.
        logger.warning(
            "Cannot read %s in %s (%r), so no downgrade check is possible for "
            "this profile.",
            LAST_VERSION_FILE,
            profile_dir,
            recorded[:80],
        )
    return version


def version_of(executable: str) -> BrowserBuild | None:
    """What ``executable --version`` reports, or None if it will not say.

    Asked of the binary rather than read from Playwright's ``browsers.json``,
    which records what the *manifest* says should be installed. Those disagree
    exactly when it matters: a half-finished or hand-copied install leaves the
    manifest describing a revision the directory does not hold, and the binary
    is the thing that is going to open the profile.

    Never asked at all where asking would start a browser instead of answering:
    see :func:`a_version_can_be_asked_for`.

    The timeout below is a real bound only on POSIX, which is the same platform
    line. CPython's ``run()`` follows its kill with a plain ``wait()`` there,
    but with an unbounded ``communicate()`` on Windows, so a wrapper that
    spawned a detached child holding the stdout pipe would be waited for rather
    than us.
    """
    # Also checked by the caller, and the two are not the same check even
    # though they read alike. This one guards the dangerous act at the moment
    # it would happen, so a future caller that forgets cannot start a browser
    # on somebody's own profile; the caller's is about staying quiet. Do not
    # collapse them into one.
    if not a_version_can_be_asked_for():
        return None

    try:
        finished = subprocess.run(
            [executable, "--version"],
            capture_output=True,
            text=True,
            # `text=True` decodes strictly, and a `CHROME_PATH` wrapper is free
            # to emit whatever bytes it likes. A `UnicodeDecodeError` is neither
            # an `OSError` nor a `SubprocessError`, so it would escape the guard
            # entirely and kill the launch -- the opposite of failing open.
            errors="replace",
            # Never the parent's stdin. Under the stdio transport that handle is
            # the JSON-RPC channel, and a wrapper script that reads a line would
            # eat an MCP message on its way past and then block until the
            # timeout below.
            stdin=subprocess.DEVNULL,
            timeout=_VERSION_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("Could not ask %s for its version: %s", executable, exc)
        return None

    build = parse_build(finished.stdout)
    if build is None:
        logger.debug(
            "No version in the output of %s --version: %r",
            executable,
            finished.stdout.strip()[:200],
        )
    return build


def refuse_a_downgrade(profile_dir: Path, executable: str | None) -> None:
    """Raise if *executable* is older than the browser that last wrote *profile_dir*.

    A forward update is allowed and always has been: Chromium migrates a profile
    it opens, and refusing that would make every browser upgrade a manual step.
    Only the other direction is refused.

    The whole version is compared, not the major. Chrome's own downgrade
    detection compares the full version, and the schema floors that make a
    downgrade lossy are raised on point releases as readily as on majors, so a
    major-only rule would wave through the case it is meant to catch.

    Only within one product line, though. ``Last Version`` records a number and
    no product, so a comparison across two products compares two different
    numbering schemes and means nothing. See :data:`_COMPARABLE_PRODUCTS`.
    """
    if executable is None:
        return

    if not a_version_can_be_asked_for():
        # Before the marker is even read, so that Windows is silent rather than
        # warning on every single start about a check it can never perform.
        logger.debug(
            "No downgrade check on this platform: a browser cannot be asked "
            "for its version without starting it."
        )
        return

    written_by = profile_was_written_by(profile_dir)
    if written_by is None:
        return

    # Only once there is something to compare against. On a fresh profile this
    # skips the subprocess entirely rather than spending it to learn nothing.
    about_to_run = version_of(executable)
    if about_to_run is None:
        # A warning, unlike the two silent returns above it, and unlike the
        # comparable-product return below. This is the branch where half the
        # evidence exists and is about to be destroyed: the marker was read,
        # the launch proceeds, and the older browser rewrites that marker down
        # to its own number on the way out. The guard can never fire on this
        # profile again, and without a line here the eventual "session
        # expired" has nothing anywhere to connect it to. The default log
        # level is WARNING, so anything quieter is invisible where it matters.
        logger.warning(
            "Could not tell which version %s is, so this profile's recorded "
            "%s cannot be checked against it. If the session stops working "
            "after this, an older browser may have opened a newer profile.",
            executable,
            _render(written_by),
        )
        return

    if not is_comparable(about_to_run.product):
        # Info rather than a warning, because this one follows directly from an
        # explicit CHROME_PATH pointing at a browser outside the Chrome family.
        # It is the configuration behaving as documented, not a surprise.
        logger.info(
            "Not checking %s %s against this profile's recorded %s: the number "
            "in Last Version carries no product, and only Chrome-family "
            "versions can be compared against it.",
            about_to_run.product,
            _render(about_to_run.version),
            _render(written_by),
        )
        return

    if about_to_run.version >= written_by:
        return

    raise BrowserDowngradeError(
        profile_version=_render(written_by),
        browser_version=_render(about_to_run.version),
        browser_product=about_to_run.product,
        profile_dir=profile_dir,
    )


def _render(version: tuple[int, ...]) -> str:
    return ".".join(str(part) for part in version)
