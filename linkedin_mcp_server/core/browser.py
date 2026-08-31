"""Browser lifecycle management using Patchright with persistent context."""

import asyncio
import contextlib
import json
import logging
import os
from pathlib import Path
from collections.abc import Coroutine, Mapping
from typing import Any, TypeVar

from patchright.async_api import (
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)

from linkedin_mcp_server.common_utils import (
    harden_linkedin_tree,
    secure_mkdir,
    secure_write_text,
)

from linkedin_mcp_server.browser_downgrade import refuse_a_downgrade
from linkedin_mcp_server.exceptions import (
    BrowserDowngradeError,
    BrowserShutdownUnconfirmedError,
)
from linkedin_mcp_server.hidden_target import (
    attaching_to_other_targets,
    hidden_target_is_supported,
    open_hidden_page,
)
from linkedin_mcp_server.process_tree import (
    WindowsJob,
    contain_browser_launch,
    drain_browser_process_marker,
    forget_browser_process_marker,
    new_browser_process_marker,
    remember_detached_process_groups,
)

from .exceptions import NetworkError, ProxyConnectionError

logger = logging.getLogger(__name__)

T = TypeVar("T")

_DEFAULT_USER_DATA_DIR = Path.home() / ".linkedin-mcp" / "profile"
_PRIVATE_FILE_MODE = 0o600
_CLEANUP_TIMEOUT_SECONDS = 10


async def await_deferring_cancels(coro: Coroutine[Any, Any, T]) -> tuple[T, bool]:
    """Await *coro* to completion, holding back cancels until it finishes.

    Mirrors ``session_state.run_deferring_cancels``. A bare ``shield`` is not
    enough: it re-raises on the *next* cancel, discarding the result. Everywhere
    this is used that result decides whether a browser is provably gone, so
    losing it would let a caller hand the profile on with Chromium possibly
    still running. Overlapping cancels are real -- a tool timeout racing a
    server shutdown -- so the loop keeps waiting however many arrive.

    Returns the result and whether a cancel arrived, so the caller can finish
    settling the profile and then re-raise whatever it was already handling.
    Nothing is swallowed here; the decision belongs to the caller.
    """
    task = asyncio.ensure_future(coro)
    cancelled = False
    while True:
        try:
            return await asyncio.shield(task), cancelled
        except asyncio.CancelledError:
            cancelled = True
            if task.done():
                return task.result(), True


class BrowserManager:
    """Async context manager for Patchright browser with persistent profile.

    Session persistence is handled automatically by the persistent browser
    context -- all cookies, localStorage, and session state are retained in
    the ``user_data_dir`` between runs.
    """

    def __init__(
        self,
        user_data_dir: str | Path = _DEFAULT_USER_DATA_DIR,
        headless: bool = True,
        slow_mo: int = 0,
        viewport: dict[str, int] | None = None,
        **launch_options: Any,
    ):
        # ``launch_options`` is spread straight into the context options, so a
        # stray ``user_agent`` here would reach Patchright and take effect
        # without anything in between noticing. Refused rather than dropped:
        # this is the one funnel every browser in the process goes through, and
        # an override that fails loudly cannot come back by accident. See the
        # browser identity rules in AGENTS.md.
        if "user_agent" in launch_options:
            raise TypeError(
                "BrowserManager does not accept a user_agent. The browser "
                "reports its own identity; an override changes the string but "
                "not the client hints, and never reaches service workers."
            )

        # Same funnel, same hazard. ``_geometry()`` is spread *before*
        # ``launch_options``, so a stray ``no_viewport`` would win: passing
        # ``no_viewport=False`` on a headed launch puts the emulated screen back
        # and restores the window-larger-than-screen contradiction, and passing
        # ``no_viewport=True`` on a headless one sends both keys at once.
        # Nothing produces this today; it is refused so it cannot start.
        if "no_viewport" in launch_options:
            raise TypeError(
                "BrowserManager decides no_viewport from the launch mode. Pass "
                "headless= instead: a headed window must report its real size, "
                "and a headless one needs an explicit viewport."
            )

        self.user_data_dir = str(Path(user_data_dir).expanduser())
        self.headless = headless
        self.slow_mo = slow_mo
        # Kept as passed, including ``None``. The old ``viewport or {...}``
        # meant "no viewport" could not be expressed at all, which is what
        # forced an emulated screen onto a headed window and produced the
        # measured contradiction: an outer window of 805 pixels standing on a
        # screen the same browser reported as 720 tall.
        self.viewport = viewport
        self.launch_options = launch_options
        self._process_marker, self._process_environment = new_browser_process_marker()

        self._playwright: Playwright | None = None
        #: This launch's Windows containment, or None on POSIX and before the
        #: driver exists. On Windows it is the whole of the attribution: there
        #: is no environment marker to scan for, so a close with nothing here
        #: cannot prove anything and says so. See ``contain_browser_launch``.
        self._containment: WindowsJob | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._is_authenticated = False
        #: Set when a headed launch was attempted and refused, which is the only
        #: reliable way to learn that this machine has nowhere to put a window.
        #: Per instance rather than per process, deliberately: a fresh manager
        #: is built for each browser, so this saves a second doomed attempt
        #: within one launch without cacheing a machine-wide answer that could
        #: go stale when somebody logs into a desktop session.
        self._no_window_available = False
        # False until a teardown proves Chromium exited. Pessimistic by default:
        # a launch that is cancelled before close runs must not read as clean.
        # Cleared again by every new launch, so it never speaks for a browser
        # that is currently running. See ``_begin_a_launch``.
        self._close_confirmed = False
        # The same answer, kept across calls rather than per call. ``close()``
        # takes the handles before its first await, so a cancel landing in the
        # middle leaves an object with nothing left to close and a Chromium that
        # may still be running. Answering the retry from that emptiness is what
        # released the lease and deleted the runtime directory under a live
        # browser. These two say which emptiness it is: nothing was ever
        # started, or a teardown began and never finished. Both belong to one
        # launch: ``_begin_a_launch`` decides what the next one may inherit.
        self._close_proven = False
        self._close_interrupted = False

    async def __aenter__(self) -> "BrowserManager":
        await self.start()
        return self

    async def __aexit__(
        self, exc_type: object, exc_val: object, exc_tb: object
    ) -> None:
        # Recorded rather than returned: ``__aexit__`` cannot report it, and a
        # caller that hands the profile on afterwards must be able to tell
        # whether Chromium actually exited. See :attr:`close_confirmed`.
        # Cleared first so a cancellation mid-teardown leaves it false rather
        # than claiming a shutdown that never completed.
        self._close_confirmed = False
        # Deferred rather than abandoned: the login path reads
        # :attr:`close_confirmed` from a ``finally`` and releases the profile on
        # it, so a cancel landing here would drop the one answer that decides
        # whether the profile may go. The cancel is re-raised, not swallowed.
        confirmed, cancelled = await await_deferring_cancels(self.close())
        self._close_confirmed = confirmed
        if cancelled:
            raise asyncio.CancelledError

    @property
    def _windowless(self) -> bool:
        """Whether this launch hides its page in a target rather than a mode.

        Both conditions, and the second is not a preference. Asking for no
        visible window is not enough on a machine that cannot open one: a headed
        launch there fails outright, so the only way to run at all is Chromium's
        headless mode, and the browser then says so on every surface. That is a
        loss worth announcing rather than hiding, which is why it is logged.
        """
        return (
            self.headless
            and hidden_target_is_supported()
            and not self._no_window_available
        )

    def _geometry(self) -> dict[str, Any]:
        """The viewport options, decided by the mode this browser actually runs in.

        This lives here rather than in ``build_launch_options`` because only
        this object knows the answer. The builder is a pure function of the
        configuration, and the configuration says ``headless=True`` by default
        even when the manual login is about to launch headed -- the login passes
        ``headless=False`` directly. A builder reading the configuration would
        get it wrong for exactly the launch that puts a window on screen.

        Headed gets no viewport at all, so the window reports the size it really
        is. Headless keeps an explicit one, because a headless browser with
        ``no_viewport`` collapses its screen to 800x600, which is its own
        oddity.
        """
        if self.headless:
            return {"viewport": self.viewport or {"width": 1280, "height": 720}}
        return {"no_viewport": True}

    def _executable_about_to_run(self) -> str | None:
        """The binary this launch will use, or None if it cannot be named.

        An explicit ``executable_path`` is the operator's own choice and wins,
        exactly as it does inside Playwright (``_prepareToLaunch``). Otherwise
        the registry answers, and with ``channel="chromium"`` set by
        ``build_launch_options`` that is the same full Chrome for Testing in
        both modes: ``getExecutableName()`` returns the channel unchanged
        before it ever reaches the headless/shell split, and
        ``executablePath()`` looks up the same registry entry. (Not via
        ``isChromiumAlias``, which holds only ``chrome-for-testing``.)

        None on anything unexpected, deliberately. Not being able to name the
        binary says nothing about whether it is older than the profile, and the
        launch that follows reports a missing browser far better than a guard
        pretending to know one is there.
        """
        custom = self.launch_options.get("executable_path")
        if custom:
            return str(custom)
        playwright = self._playwright
        if playwright is None:
            return None
        try:
            registry_path = playwright.chromium.executable_path
        except Exception as exc:
            logger.debug("Could not resolve the browser executable: %s", exc)
            return None
        # The registry answers with `... || ""` when nothing is installed, and
        # an empty string is not a name. Passed on it would buy a doomed
        # `subprocess.run(["", "--version"])` and then a warning naming no
        # binary at all. The launch below is what reports a missing browser,
        # and it says so with the path.
        return str(registry_path) if registry_path else None

    async def _start_contained_driver(self) -> Playwright:
        """Start one Patchright driver and contain what it is about to spawn.

        The containment has to exist between these two events and nowhere else.
        Windows Job membership is inherited at process creation, so the Node
        driver must be in the Job before it spawns Chromium -- and it spawns
        nothing until ``launch_persistent_context``, which is the next thing
        the caller does.

        A driver that cannot be contained is stopped here rather than handed
        back. It has launched nothing yet, so a proved stop is a complete
        cleanup and leaves the profile untouched; a stop that fails keeps the
        handle for ``close()`` to deal with.
        """
        # The attach flag only where a hidden target is actually going to be
        # created. It has to exist before the driver subprocess does, and it
        # then lives in that process for its whole lifetime -- restoring it here
        # afterwards does nothing to the child. So a visible login, or a
        # platform that falls back to real headless, would otherwise spend its
        # entire run promoting extension and other `other` targets into
        # `context.pages` for no reason.
        if self._windowless:
            with attaching_to_other_targets():
                driver = await async_playwright().start()
        else:
            driver = await async_playwright().start()
        self._playwright = driver
        try:
            self._containment = contain_browser_launch(driver)
        except BaseException:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(driver.stop(), timeout=_CLEANUP_TIMEOUT_SECONDS)
                self._playwright = None
            raise
        return driver

    def _begin_a_launch(self) -> None:
        """Take this manager into a new launch, or refuse to.

        A second ``start()`` is part of the contract: the error above tells
        callers to close first, which is only an instruction if closing then
        lets them open again. What the new launch may not do is inherit the
        previous one's verdict. ``_close_proven`` answers every later
        ``close()`` from its own record without touching a handle, so a restart
        that left it standing would hand back ``True`` while the *new* context
        and driver are still live: no Patchright teardown, no OS-level drain,
        and a caller free to release or delete the profile with Chromium on it.

        A teardown that was never proved is refused rather than reset. That is
        the whole meaning of ``_close_interrupted``: the earlier browser may
        still be sitting on this profile, and opening a second one there is the
        concurrent-profile corruption this module exists to prevent. No state
        cleared here can make the first browser gone, so the only safe answer
        is to say so.

        What crosses the boundary and what does not:

        * The marker is minted fresh. The old one was handed to
          ``forget_browser_process_marker`` on the strength of the proof, so a
          second Chromium carrying it would announce itself under a launch this
          process has already written off. A new one also re-registers with the
          crash guardian, which is exactly what a new manager would do.
        * The containment is dropped. A proved Windows drain empties the Job
          and closes its handle (``WindowsJob.wait_until_empty``), so what is
          left is spent, and this attribute is documented as describing *this*
          launch.
        * ``_is_authenticated`` goes back to false. It says startup
          authentication succeeded for the page this manager hands out, and
          ``drivers.browser.validate_session`` skips its live check while it is
          true. Carried over, it would answer for a context nobody validated.
        * ``_no_window_available`` deliberately survives. It records that this
          machine refused a window, which a second launch on the same machine
          would only rediscover by opening another doomed headed browser.
        """
        if self._close_interrupted:
            raise BrowserShutdownUnconfirmedError(
                "The previous browser on this profile was not proved to have "
                "exited, so another one cannot be started on it. Restart the "
                "server to recover."
            )
        if not self._close_proven:
            # Nothing has closed here, so there is nothing to hand over: either
            # this is the first launch or a previous ``start()`` is still live,
            # and the caller met the already-started error before reaching this.
            return

        # Minted before anything is assigned. A guardian that has gone makes
        # this raise, and a manager that keeps its proved-closed state is a
        # better thing to leave behind than one that has half-entered a launch
        # it never made.
        marker, environment = new_browser_process_marker()
        self._process_marker = marker
        self._process_environment = environment
        self._close_proven = False
        self._close_confirmed = False
        self._is_authenticated = False
        self._containment = None

    async def start(self) -> None:
        """Start Patchright and launch persistent browser context."""
        if self._context is not None:
            raise RuntimeError("Browser already started. Call close() first.")
        # Before the driver, and before anything that could spawn a process:
        # every launch-specific answer this object carries is either replaced
        # here or refuses the launch outright.
        self._begin_a_launch()
        try:
            driver = await self._start_contained_driver()

            # Before the profile directory is so much as created, and long
            # before it is opened. The driver had to start first, because only
            # it can say which binary the registry will hand this launch, but
            # nothing beyond that has happened yet -- a refusal here leaves the
            # profile exactly as it was found, which is the whole point of
            # refusing. Off the event loop: asking a binary for its version
            # spawns a process and waits ~35 ms for it.
            await asyncio.to_thread(
                refuse_a_downgrade,
                Path(self.user_data_dir),
                self._executable_about_to_run(),
            )

            secure_mkdir(Path(self.user_data_dir))
            harden_linkedin_tree(Path(self.user_data_dir))

            context_options: dict[str, Any] = {
                # Headed wherever a window can exist, in both public modes.
                # ``self.headless`` keeps its meaning -- "no visible window" --
                # but it is no longer how that is achieved, because Chromium's
                # headless *mode* is what makes the browser announce itself. A
                # windowless page comes from a hidden target instead.
                #
                # Where no display exists there is no choice: a headed launch
                # dies before any of that can happen. See ``_windowless``.
                "headless": self.headless and not self._windowless,
                "slow_mo": self.slow_mo,
                **self._geometry(),
                **self.launch_options,
                "locale": "en-US",
            }
            configured_environment = context_options.get("env")
            if configured_environment is None:
                browser_environment = dict(os.environ)
            elif isinstance(configured_environment, Mapping):
                browser_environment = dict(configured_environment)
            else:
                raise TypeError("Browser launch env must be a mapping")
            browser_environment.update(self._process_environment)
            context_options["env"] = browser_environment

            # No ``user_agent`` here, deliberately. Patchright leaves the client
            # hints reporting the real browser, so an override contradicts
            # itself on the first surface anyone checks, and it never reaches
            # service workers at all. See the browser identity rules in
            # AGENTS.md and the measurements in docs/browser-fingerprint.md.
            try:
                self._context = await driver.chromium.launch_persistent_context(
                    self.user_data_dir,
                    **context_options,
                )
            except Exception as exc:
                # A launch can leave detached Chromium behind even when Patchright
                # never returns a context. Retain its group while the Node driver's
                # ancestry still makes it discoverable.
                remember_detached_process_groups(self._process_marker)
                # A headed launch needs somewhere to put a window, and whether
                # this machine has one cannot be decided from the platform name
                # alone: a Mac reached over SSH, a launchd daemon, or a CI
                # runner with no GUI session all look like macOS and all refuse
                # to open one. Rather than enumerate those, let the attempt
                # answer it -- that is the one check that cannot be wrong about
                # a case nobody thought of.
                #
                # Narrow on purpose: only when a window was asked for and only
                # once, so a genuine launch failure still surfaces rather than
                # being retried into a different error.
                if not self._windowless:
                    raise
                logger.warning(
                    "Could not start a browser with a window (%s), so Chromium "
                    "runs in headless mode and will identify itself as "
                    "HeadlessChrome on every surface.",
                    type(exc).__name__,
                )
                self._no_window_available = True

                # The driver has to be replaced, not reused. It was started
                # with the attach flag, and that flag lives in *its* process for
                # its whole life -- restoring the parent environment does
                # nothing to a child that already read it. A driver that keeps
                # promoting `other` targets would put a component extension's
                # page into `context.pages`, and the code below takes the first
                # one as the page to authenticate and scrape with.
                # Bounded, and a failure aborts the fallback. Starting another
                # Chromium while the first driver may still own this profile is
                # concurrent access, so only a confirmed driver stop may proceed.
                try:
                    await asyncio.wait_for(
                        driver.stop(), timeout=_CLEANUP_TIMEOUT_SECONDS
                    )
                except Exception as stop_exc:
                    logger.warning(
                        "The refused driver did not stop (%s); aborting the fallback.",
                        type(stop_exc).__name__,
                    )
                    raise exc from None
                self._playwright = None

                # A stopped driver proves the Node process and the leader it
                # spawned are gone, and nothing else. Chromium is spawned
                # detached into its own group, so a refused headed launch can
                # leave a tree standing that outlives its driver -- and the
                # fallback below reopens the very same profile directory. That
                # is concurrent profile access, which is the corruption this
                # whole module exists to prevent, so this launch's residue is
                # drained before a second driver is allowed to exist.
                #
                # Off the loop: this scans processes and can sleep.
                drained, cancelled = await await_deferring_cancels(
                    asyncio.to_thread(
                        drain_browser_process_marker,
                        self._process_marker,
                        containment=self._containment,
                    )
                )
                if not drained:
                    # Recorded so the teardown below cannot answer from empty
                    # handles: this launch began a shutdown and did not finish
                    # it, and only a proved drain may say otherwise.
                    self._close_interrupted = True
                    logger.error(
                        "The refused headed launch left processes on the "
                        "profile; aborting the headless fallback rather than "
                        "opening a second browser on it."
                    )
                    raise exc from None
                self._containment = None
                if cancelled:
                    # The drain was worth finishing; the fallback is not. Held
                    # back only until this launch was proved gone, and re-raised
                    # now rather than opening a browser nobody is waiting for.
                    raise asyncio.CancelledError
                driver = await self._start_contained_driver()

                context_options["headless"] = True
                context_options.pop("no_viewport", None)
                context_options.update(self._geometry())
                try:
                    self._context = await driver.chromium.launch_persistent_context(
                        self.user_data_dir,
                        **context_options,
                    )
                except Exception as retry_exc:
                    # Logged before it is discarded. The raise below keeps the
                    # first error because that is the one that says what went
                    # wrong, but losing the second entirely would leave whoever
                    # debugs this unable to see that the fallback was even
                    # tried, let alone how it failed.
                    logger.warning(
                        "The headless fallback did not start either: %s: %s",
                        type(retry_exc).__name__,
                        retry_exc,
                    )
                    # The retry is a chance, not a cover-up. If the browser
                    # will not start either way the problem was never the
                    # window, and the first error is the one that says what it
                    # actually was.
                    raise exc from None

            # Patchright starts Chromium in its own POSIX process group. Capture
            # that group before page setup or authentication can fail and before
            # the Node driver can exit and reparent it.
            remember_detached_process_groups(self._process_marker)
            logger.info(
                "Persistent browser launched (headless=%s, user_data_dir=%s)",
                self.headless,
                self.user_data_dir,
            )
            if self.headless and not self._windowless:
                logger.info(
                    "Chromium runs in headless mode on this platform and so "
                    "identifies itself as HeadlessChrome on every surface. A "
                    "windowless page needs a browser that survives losing its "
                    "last window, which is measured only on macOS."
                )

            startup = (
                self._context.pages[0]
                if self._context.pages
                else await self._context.new_page()
            )
            if self._windowless:
                # Fails closed. Falling back to real headless would restore the
                # token the caller believes is gone, and falling back to the
                # visible window would put one on their screen unannounced.
                self._page = await open_hidden_page(self._context, startup)
            else:
                self._page = startup

            logger.info("Browser context and page ready")

        except BaseException as e:
            # A failure before a context was returned can still have spawned a
            # detached browser. This is idempotent with the two launch checkpoints
            # above and must run before cleanup can make ancestry disappear.
            remember_detached_process_groups(self._process_marker)
            # BaseException so a cancelled launch is cleaned up too: Chromium may
            # already be running, and leaving it would hold the profile.
            #
            # The result is recorded, not discarded: this is the only close that
            # can prove a partially launched Chromium exited. A caller closing
            # again would get True from the already-cleared handles and could
            # then release or delete the profile with the browser still on it.
            # Shielded, and retried on further cancels: overlapping cancels are
            # real (a tool timeout racing server shutdown), and a second one
            # landing on the shield would discard the very result that decides
            # whether the profile may be handed on.
            closed, _ = await await_deferring_cancels(self.close())
            # The same field ``__aexit__`` writes, because this is the same
            # answer and there is no exit to write it: a failing ``start()``
            # means ``__aenter__`` raised, and Python then never calls
            # ``__aexit__``. Without this the one close that *did* prove the
            # profile free is invisible to the caller, which reads
            # :attr:`close_confirmed` from a ``finally`` and keeps the profile
            # on it. Measured against an unusable ``CHROME_PATH``: the drain
            # proved no Chromium was left, and the login still kept the lease,
            # skipped the guardian release and left the retired session in
            # quarantine for the life of the process.
            self._close_confirmed = closed
            if isinstance(e, BrowserDowngradeError):
                # Through untouched, and ahead of the shutdown check rather than
                # after it. Both wrappings below carry a recovery that would
                # make this worse: `NetworkError` is read downstream as a
                # missing binary and reinstalls the same old revision, and the
                # auth path rotates the profile away, destroying an intact
                # session whose only fault is being newer than the browser
                # asking for it.
                #
                # `BrowserShutdownUnconfirmedError` would be the third, and it
                # is the reason for the ordering. This refusal happens before
                # `launch_persistent_context`, so no Chromium ever existed and
                # nothing can be holding the profile -- only the driver process
                # is up. Yet a driver that misses its 10 s stop would replace an
                # actionable message with a generic one *and* leave the caller
                # marking the profile busy for the rest of this process's life,
                # over a profile that was never opened. The close still runs;
                # only its verdict is set aside, and only here.
                raise
            if not closed:
                raise BrowserShutdownUnconfirmedError(
                    "The browser failed to start and did not shut down cleanly, "
                    "so the profile is kept. Restart the server to recover."
                ) from e
            if isinstance(e, Exception):
                # A rejected proxy (bad scheme, SOCKS auth) fails at launch
                # rather than on navigation. Reported as itself, with the
                # credentials stripped and the raw cause dropped: the top-level
                # handlers log the whole cause chain.
                from .proxy_errors import is_proxy_error, redact_proxy_credentials

                if is_proxy_error(e):
                    raise ProxyConnectionError(
                        f"Failed to start browser: {redact_proxy_credentials(str(e))}"
                    ) from None
                raise NetworkError(
                    f"Failed to start browser: {redact_proxy_credentials(str(e))}"
                ) from e
            raise

    async def close(self) -> bool:
        """Close persistent context and cleanup resources.

        Returns whether shutdown was *confirmed*. Both Patchright cleanup steps
        are bounded and their failures swallowed, so a wedged Chromium can still
        be running when they return. Callers that hand the profile to another
        process on the strength of a close must check this: releasing it while
        Chromium is alive reintroduces the concurrent-profile corruption.

        The verdict comes from the OS-level drain rather than from those steps,
        and the drain runs however they went. It is the only thing here that can
        both see the detached tree and end it, so a close that fails at the API
        is exactly the close that needs it most.

        The answer is sticky in both directions, for the launch it belongs to.
        Once a teardown has been proved complete every later call agrees, and
        until then no call may claim it: an interrupted close is retried rather
        than believed, and a retry that finds the handles already gone still has
        to prove the browser is. A ``start()`` after a proved close opens a new
        launch and clears that record with everything else it owns; see
        :meth:`_begin_a_launch`.
        """
        if self._close_proven:
            # Proved once, and the marker was forgotten on the strength of it.
            # Draining again would scan the machine for something no process
            # can be carrying any more.
            return True

        context = self._context
        playwright = self._playwright
        self._context = None
        self._page = None
        self._playwright = None
        # Set before the first await below and cleared only by a proved drain,
        # so a cancel escaping any of them leaves the interruption recorded.
        resuming = self._close_interrupted
        self._close_interrupted = True
        confirmed = True

        if (
            context is None
            and playwright is None
            and self._containment is None
            and not resuming
        ):
            # Nothing was ever handed out, so no driver ran and no Chromium can
            # carry this launch's marker. The other emptiness -- a close that
            # took the handles and was cut off -- goes through the drain below.
            # The containment is checked as well as the handles, because on
            # Windows it is the attribution and the handles are not: leaving a
            # live Job unexamined here would claim a shutdown from the one
            # object that could have disproved it.
            self._close_proven = True
            self._close_interrupted = False
            forget_browser_process_marker(self._process_marker)
            return True

        # Bound each cleanup step. A wedged Chromium (stale SingletonLock,
        # sandbox stall, X-less host) can hang context.close() / playwright.stop()
        # indefinitely; without these timeouts a caller that cancels close()
        # (e.g. asyncio.wait_for on the auto-import) would block past its own
        # budget while awaiting the hung cleanup.
        if context is not None:
            try:
                await asyncio.wait_for(
                    context.close(), timeout=_CLEANUP_TIMEOUT_SECONDS
                )
            except TimeoutError:
                confirmed = False
                logger.error(
                    "Timed out closing browser context after %ss",
                    _CLEANUP_TIMEOUT_SECONDS,
                )
            except Exception as exc:
                confirmed = False
                logger.error("Error closing browser context: %s", exc)

        if playwright is not None:
            try:
                await asyncio.wait_for(
                    playwright.stop(), timeout=_CLEANUP_TIMEOUT_SECONDS
                )
            except TimeoutError:
                confirmed = False
                logger.error(
                    "Timed out stopping playwright after %ss",
                    _CLEANUP_TIMEOUT_SECONDS,
                )
            except Exception as exc:
                confirmed = False
                logger.error("Error stopping playwright: %s", exc)

        logger.info("Browser closed")
        # The API's own verdict is not the browser's. Patchright waits for the
        # leader it spawned and for its temporary directories, and signals the
        # detached group only when that graceful attempt fails, so a close that
        # returns cleanly says nothing about the rest of the tree.
        #
        # Run whatever the two steps above did, and take the answer from here
        # alone. A context that would not close is precisely when a tree is most
        # likely left standing, so skipping the one check that could see it
        # would leave the profile's fate to the API that just failed to report
        # it -- and the drain is not a report but a kill, which is the only
        # thing that can still end that tree. It cuts the other way too: the
        # Node driver carries no marker, so a driver that misses its stop while
        # Chromium is provably gone no longer keeps the profile busy for the
        # rest of this process's life. Off the loop: this scans processes and
        # can sleep.
        drained = await asyncio.to_thread(
            drain_browser_process_marker,
            self._process_marker,
            containment=self._containment,
        )
        if not drained:
            logger.error(
                "Browser processes from this launch are still running after "
                "close, so the shutdown stays unconfirmed."
            )
        elif not confirmed:
            logger.warning(
                "Patchright cleanup did not complete, but this launch left no "
                "processes behind, so the shutdown is confirmed."
            )
        confirmed = drained
        if confirmed:
            self._close_proven = True
            self._close_interrupted = False
            forget_browser_process_marker(self._process_marker)
        return confirmed

    @property
    def close_confirmed(self) -> bool:
        """Whether the last ``async with`` exit proved Chromium had gone.

        False means cleanup timed out or failed and the browser may still be
        running, so the profile must not be handed to anyone else. Also false
        before the first launch is torn down and again from the moment a new
        one begins, so it never speaks for a browser that is currently up.

        A ``start()`` that fails answers here too. It closes what it may have
        launched and that close can prove the profile free, but the failure
        propagates out of ``__aenter__`` and there is no ``__aexit__`` behind
        it to record the verdict.
        """
        return self._close_confirmed

    @property
    def page(self) -> Page:
        if not self._page:
            raise RuntimeError(
                "Browser not started. Use async context manager or call start()."
            )
        return self._page

    @property
    def context(self) -> BrowserContext:
        if not self._context:
            raise RuntimeError("Browser context not initialized.")
        return self._context

    async def set_cookie(
        self, name: str, value: str, domain: str = ".linkedin.com"
    ) -> None:
        if not self._context:
            raise RuntimeError("No browser context")

        await self._context.add_cookies(
            [{"name": name, "value": value, "domain": domain, "path": "/"}]
        )
        logger.debug("Cookie set: %s", name)

    @property
    def is_authenticated(self) -> bool:
        return self._is_authenticated

    @is_authenticated.setter
    def is_authenticated(self, value: bool) -> None:
        self._is_authenticated = value

    def _default_cookie_path(self) -> Path:
        return Path(self.user_data_dir).parent / "cookies.json"

    @staticmethod
    def _normalize_cookie_domain(cookie: Any) -> dict[str, Any]:
        """Normalize cookie domain for cross-platform compatibility.

        Playwright reports some LinkedIn cookies with ``.www.linkedin.com``
        domain, but Chromium's internal store uses ``.linkedin.com``.
        """
        domain = cookie.get("domain", "")
        if domain in (".www.linkedin.com", "www.linkedin.com"):
            cookie = {**cookie, "domain": ".linkedin.com"}
        return cookie

    async def export_cookies(self, cookie_path: str | Path | None = None) -> bool:
        """Export LinkedIn cookies to a portable JSON file."""
        if not self._context:
            logger.warning("Cannot export cookies: no browser context")
            return False

        path = Path(cookie_path) if cookie_path else self._default_cookie_path()
        try:
            # Bounded like the teardown steps below it, and for a stronger
            # reason. On close this runs *before* them, with the singleton
            # already cleared and the profile lease still held, inside a section
            # that defers cancellation until it finishes. A protocol call that
            # never answers therefore strands the profile before anything
            # bounded is reached, and raises nothing for anyone to act on: no
            # close result, no exception, no stand-down. Losing an export costs
            # the Docker cookie file this run, which the next close rewrites.
            all_cookies = await asyncio.wait_for(
                self._context.cookies(), timeout=_CLEANUP_TIMEOUT_SECONDS
            )
            cookies = [
                self._normalize_cookie_domain(c)
                for c in all_cookies
                if "linkedin.com" in c.get("domain", "")
            ]
            secure_mkdir(path.parent)
            harden_linkedin_tree(path.parent)
            secure_write_text(
                path, json.dumps(cookies, indent=2), mode=_PRIVATE_FILE_MODE
            )
            logger.info("Exported %d LinkedIn cookies to %s", len(cookies), path)
            return True
        except Exception:
            logger.exception("Failed to export cookies")
            return False

    async def export_storage_state(
        self, path: str | Path, *, indexed_db: bool = True
    ) -> bool:
        """Export the current browser storage state for diagnostics and recovery."""
        if not self._context:
            logger.warning("Cannot export storage state: no browser context")
            return False

        storage_path = Path(path)
        secure_mkdir(storage_path.parent)
        harden_linkedin_tree(storage_path.parent)
        try:
            await self._context.storage_state(
                path=storage_path,
                indexed_db=indexed_db,
            )
            # Playwright writes the file with default umask; tighten it.
            if os.name != "nt" and storage_path.exists():
                storage_path.chmod(_PRIVATE_FILE_MODE)
            logger.info(
                "Exported runtime storage snapshot to %s (indexed_db=%s)",
                storage_path,
                indexed_db,
            )
            return True
        except Exception:
            logger.exception("Failed to export storage state to %s", storage_path)
            return False

    _BRIDGE_COOKIE_PRESETS = {
        "bridge_core": frozenset(
            {
                "li_at",
                "li_rm",
                "JSESSIONID",
                "bcookie",
                "bscookie",
                "liap",
                "lidc",
                "li_gc",
                "lang",
                "timezone",
                "li_mc",
            }
        ),
        "auth_minimal": frozenset(
            {
                "li_at",
                "JSESSIONID",
                "bcookie",
                "bscookie",
                "lidc",
            }
        ),
    }

    @classmethod
    def _bridge_cookie_names(
        cls, preset_name: str | None = None
    ) -> tuple[str, frozenset[str]]:
        preset_name = (
            preset_name
            or os.getenv(
                "LINKEDIN_DEBUG_BRIDGE_COOKIE_SET",
                "auth_minimal",
            ).strip()
            or "auth_minimal"
        )
        preset = cls._BRIDGE_COOKIE_PRESETS.get(preset_name)
        if preset is None:
            logger.warning(
                "Unknown LINKEDIN_DEBUG_BRIDGE_COOKIE_SET=%r, falling back to auth_minimal",
                preset_name,
            )
            preset_name = "auth_minimal"
            preset = cls._BRIDGE_COOKIE_PRESETS[preset_name]
        return preset_name, preset

    async def import_cookies(
        self,
        cookie_path: str | Path | None = None,
        *,
        preset_name: str | None = None,
    ) -> bool:
        """Import the portable LinkedIn bridge cookie subset.

        Fresh browser-side cookies are preserved. The imported subset is the
        smallest known set that can reconstruct a usable authenticated page in
        a fresh profile.
        """
        if not self._context:
            logger.warning("Cannot import cookies: no browser context")
            return False

        path = Path(cookie_path) if cookie_path else self._default_cookie_path()
        if not path.exists():
            logger.debug("No portable cookie file at %s", path)
            return False

        try:
            all_cookies = json.loads(path.read_text())
            if not all_cookies:
                logger.debug("Cookie file is empty")
                return False

            resolved_preset_name, bridge_cookie_names = self._bridge_cookie_names(
                preset_name
            )

            cookies = [
                self._normalize_cookie_domain(c)
                for c in all_cookies
                if "linkedin.com" in c.get("domain", "")
                and c.get("name") in bridge_cookie_names
            ]

            has_li_at = any(c.get("name") == "li_at" for c in cookies)
            if not has_li_at:
                logger.warning("No li_at cookie found in %s", path)
                return False

            await self._context.add_cookies(
                cookies  # ty: ignore[invalid-argument-type]
            )
            logger.info(
                "Imported %d LinkedIn bridge cookies from %s (preset=%s, li_at=%s): %s",
                len(cookies),
                path,
                resolved_preset_name,
                has_li_at,
                ", ".join(c["name"] for c in cookies),
            )
            return True
        except Exception:
            logger.exception("Failed to import cookies from %s", path)
            return False

    def cookie_file_exists(self, cookie_path: str | Path | None = None) -> bool:
        """Check if a portable cookie file exists."""
        path = Path(cookie_path) if cookie_path else self._default_cookie_path()
        return path.exists()
