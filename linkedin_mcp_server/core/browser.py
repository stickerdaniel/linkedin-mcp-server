"""Browser lifecycle management using Patchright with persistent context."""

import asyncio
import json
import logging
import os
from pathlib import Path
from collections.abc import Coroutine
from typing import Any

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

from .exceptions import NetworkError, ProxyConnectionError

logger = logging.getLogger(__name__)

_DEFAULT_USER_DATA_DIR = Path.home() / ".linkedin-mcp" / "profile"
_PRIVATE_FILE_MODE = 0o600
_CLEANUP_TIMEOUT_SECONDS = 10


async def _await_deferring_cancels(coro: Coroutine[Any, Any, bool]) -> bool:
    """Await *coro* to completion, holding back cancels until it finishes.

    Mirrors ``session_state.run_deferring_cancels``. A bare ``shield`` is not
    enough: it re-raises on the *next* cancel, discarding the result. Here that
    result decides whether a browser is provably gone, so losing it would let a
    caller hand the profile on with Chromium possibly still running. The cancel
    is not swallowed; the caller re-raises it.
    """
    task = asyncio.ensure_future(coro)
    while True:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.done():
                return task.result()


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

        self._playwright: Playwright | None = None
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
        self._close_confirmed = False

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
        self._close_confirmed = await self.close()

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

    async def start(self) -> None:
        """Start Patchright and launch persistent browser context."""
        if self._context is not None:
            raise RuntimeError("Browser already started. Call close() first.")
        try:
            # Only where a hidden target is actually going to be created. The
            # flag has to exist before the driver subprocess does, and it then
            # lives in that process for its whole lifetime -- restoring it here
            # afterwards does nothing to the child. So a visible login, or a
            # platform that falls back to real headless, would otherwise spend
            # its entire run promoting extension and other `other` targets into
            # `context.pages` for no reason.
            if self._windowless:
                with attaching_to_other_targets():
                    self._playwright = await async_playwright().start()
            else:
                self._playwright = await async_playwright().start()

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

            # No ``user_agent`` here, deliberately. Patchright leaves the client
            # hints reporting the real browser, so an override contradicts
            # itself on the first surface anyone checks, and it never reaches
            # service workers at all. See the browser identity rules in
            # AGENTS.md and the measurements in docs/browser-fingerprint.md.
            try:
                self._context = (
                    await self._playwright.chromium.launch_persistent_context(
                        self.user_data_dir,
                        **context_options,
                    )
                )
            except Exception as exc:
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
                # Bounded, and its failure survived, for the same reason
                # ``close()`` bounds its own cleanup: a wedged driver can hang
                # ``stop()`` indefinitely. Turning a recoverable launch into a
                # permanent hang would be the worse trade, so a driver that will
                # not stop is left behind and said so.
                try:
                    await asyncio.wait_for(
                        self._playwright.stop(), timeout=_CLEANUP_TIMEOUT_SECONDS
                    )
                except Exception as stop_exc:
                    logger.warning(
                        "The refused driver did not stop (%s); continuing with a "
                        "fresh one.",
                        type(stop_exc).__name__,
                    )
                self._playwright = await async_playwright().start()

                context_options["headless"] = True
                context_options.pop("no_viewport", None)
                context_options.update(self._geometry())
                try:
                    self._context = (
                        await self._playwright.chromium.launch_persistent_context(
                            self.user_data_dir,
                            **context_options,
                        )
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
            closed = await _await_deferring_cancels(self.close())
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

        Returns whether shutdown was *confirmed*. Both cleanup steps are bounded
        and their failures swallowed, so a wedged Chromium can still be running
        when this returns. Callers that hand the profile to another process on
        the strength of a close must check this: releasing it while Chromium is
        alive reintroduces the concurrent-profile corruption.
        """
        context = self._context
        playwright = self._playwright
        self._context = None
        self._page = None
        self._playwright = None
        confirmed = True

        if context is None and playwright is None:
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
        return confirmed

    @property
    def close_confirmed(self) -> bool:
        """Whether the last ``async with`` exit proved Chromium had gone.

        False means cleanup timed out or failed and the browser may still be
        running, so the profile must not be handed to anyone else.
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
