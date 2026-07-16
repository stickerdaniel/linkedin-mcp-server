"""Browser lifecycle management with a persistent context.

Launch/profile-path logic per engine (Patchright, Camoufox) lives in
``core.engines``; this module owns the shared lifecycle (start/close,
cookie export, storage-state export) on top of whichever engine is
configured.
"""

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any

from linkedin_mcp_server.common_utils import (
    harden_linkedin_tree,
    secure_mkdir,
    secure_write_text,
)

from .engines import ENGINES, launch_teardown_was_confirmed
from .exceptions import BrowserTeardownError, NetworkError
from .ip_monitor import get_ip_drift_monitor

logger = logging.getLogger(__name__)

_DEFAULT_USER_DATA_DIR = Path.home() / ".linkedin-mcp" / "profile"
_PRIVATE_FILE_MODE = 0o600
_CLEANUP_TIMEOUT_SECONDS = 10
_LAUNCH_TIMEOUT_SECONDS = 60


class BrowserManager:
    """Async context manager for a browser with a persistent profile.

    Session persistence is handled automatically by the persistent browser
    context -- all cookies, localStorage, and session state are retained in
    the ``user_data_dir`` between runs. ``engine`` selects the adapter (see
    ``core.engines.ENGINES``) that actually launches the browser; each
    engine resolves its own on-disk profile subdirectory via
    ``ENGINES[engine].profile_dir()``.
    """

    def __init__(
        self,
        user_data_dir: str | Path = _DEFAULT_USER_DATA_DIR,
        headless: bool = True,
        slow_mo: int = 0,
        viewport: dict[str, int] | None = None,
        user_agent: str | None = None,
        engine: str = "patchright",
        camoufox_identity_path: str | Path | None = None,
        expected_camoufox_identity_sha256: str | None = None,
        **launch_options: Any,
    ):
        self.user_data_dir = str(Path(user_data_dir).expanduser())
        self.headless = headless
        self.slow_mo = slow_mo
        self.viewport = viewport or {"width": 1280, "height": 720}
        if engine == "camoufox" and user_agent:
            # Camoufox builds a coherent Firefox fingerprint itself. A
            # Playwright-level override changes only part of that identity;
            # in particular a Chromium source UA makes the Firefox fingerprint
            # self-contradictory and detectable.
            logger.warning(
                "Ignoring user-agent override for Camoufox; using its native "
                "Firefox fingerprint identity"
            )
            self.user_agent = None
        else:
            self.user_agent = user_agent
        self.engine = engine
        self.camoufox_identity_path = (
            Path(camoufox_identity_path).expanduser()
            if camoufox_identity_path is not None
            else None
        )
        self.expected_camoufox_identity_sha256 = expected_camoufox_identity_sha256
        self.launch_options = launch_options

        # Patchright (Chromium) and Camoufox (vanilla-Playwright-driven Firefox)
        # are two different packages with structurally-identical but distinct
        # classes, so these stay loosely typed rather than pinned to one of them.
        self._playwright: Any | None = None
        self._context: Any | None = None
        self._page: Any | None = None
        self._is_authenticated = False
        self._cleanup_task: asyncio.Task[bool] | None = None
        self._teardown_complete = True

    async def __aenter__(self) -> "BrowserManager":
        await self.start()
        return self

    async def __aexit__(
        self, exc_type: object, exc_val: object, exc_tb: object
    ) -> None:
        try:
            teardown_complete = await self.close()
        except asyncio.CancelledError as exc:
            if not self.teardown_complete:
                raise BrowserTeardownError(
                    "Browser context exit was cancelled before teardown could "
                    "be confirmed; profile lock ownership is uncertain"
                ) from exc
            raise
        if not teardown_complete:
            error = BrowserTeardownError(
                "Browser teardown did not complete; profile lock ownership is uncertain"
            )
            if isinstance(exc_val, BaseException):
                raise error from exc_val
            raise error

    async def start(self) -> None:
        """Start the configured engine and launch a persistent browser context."""
        if self._context is not None or self._cleanup_task is not None:
            raise RuntimeError("Browser already started. Call close() first.")
        if not self._teardown_complete:
            raise RuntimeError(
                "Previous browser teardown was not confirmed; refusing to reuse manager"
            )

        async def _launch_and_prepare_page() -> None:
            adapter = ENGINES[self.engine]
            try:
                self._playwright, self._context = await adapter.launch(
                    user_data_dir=self.user_data_dir,
                    headless=self.headless,
                    slow_mo=self.slow_mo,
                    viewport=self.viewport,
                    user_agent=self.user_agent,
                    launch_options=self.launch_options,
                    camoufox_identity_path=self.camoufox_identity_path,
                    expected_camoufox_identity_sha256=(
                        self.expected_camoufox_identity_sha256
                    ),
                )
            except BaseException as exc:
                # The adapter starts Playwright before it can return ownership
                # to this manager. If its bounded failure cleanup did not
                # finish, remember that uncertainty even when wait_for()
                # converts the task's cancellation into a TimeoutError.
                if not launch_teardown_was_confirmed(exc):
                    self._teardown_complete = False
                raise
            self._teardown_complete = False

            logger.info(
                "Persistent browser launched (engine=%s, headless=%s, user_data_dir=%s)",
                self.engine,
                self.headless,
                self.user_data_dir,
            )

            if self._context.pages:
                self._page = self._context.pages[0]
            else:
                self._page = await self._context.new_page()

            logger.info("Browser context and page ready")

            # Only meaningful with a proxy configured -- without one there's
            # no independent egress path for the IP to "drift" from.
            if self.launch_options.get("proxy"):
                await get_ip_drift_monitor().establish_baseline(self._page)

        try:
            secure_mkdir(Path(self.user_data_dir))
            harden_linkedin_tree(Path(self.user_data_dir))
            await asyncio.wait_for(
                _launch_and_prepare_page(), timeout=_LAUNCH_TIMEOUT_SECONDS
            )
        except TimeoutError as exc:
            await self._close_after_failed_start(
                "Browser startup timed out and teardown did not complete; "
                "profile lock ownership is uncertain",
                exc,
            )
            raise NetworkError(
                "Timed out starting browser "
                f"after {_LAUNCH_TIMEOUT_SECONDS}s (engine={self.engine}, "
                f"user_data_dir={self.user_data_dir})"
            ) from exc
        except asyncio.CancelledError as exc:
            # Tool/server timeouts may cancel startup after the adapter has
            # already returned a live context (for example while creating the
            # first page). BrowserManager owns those resources at that point.
            await self._close_after_failed_start(
                "Browser startup was cancelled and teardown did not complete; "
                "profile lock ownership is uncertain",
                exc,
            )
            raise
        except Exception as e:
            await self._close_after_failed_start(
                "Browser startup failed and teardown did not complete; "
                "profile lock ownership is uncertain",
                e,
            )
            raise NetworkError(f"Failed to start browser: {e}") from e

    async def _close_after_failed_start(
        self, message: str, cause: BaseException
    ) -> None:
        """Convert uncertain/repeatedly-cancelled startup cleanup to one type."""
        try:
            teardown_complete = await self.close()
        except asyncio.CancelledError:
            if not self.teardown_complete:
                raise BrowserTeardownError(message) from cause
            raise
        if not teardown_complete:
            raise BrowserTeardownError(message) from cause

    async def close(self) -> bool:
        """Close persistent resources and report whether teardown completed.

        The in-memory references are always cleared so repeated calls remain
        idempotent. A ``False`` result tells profile owners not to delete or
        reopen the directory: a timed-out driver may still hold its lock.
        """
        if self._cleanup_task is None:
            context = self._context
            playwright = self._playwright
            self._context = None
            self._page = None
            self._playwright = None

            if context is None and playwright is None:
                return self._teardown_complete

            self._cleanup_task = asyncio.create_task(
                self._cleanup_resources(context, playwright),
                name=f"browser-cleanup-{self.engine}",
            )

        cleanup_task = self._cleanup_task
        try:
            # Shield the owned teardown so cancellation of a tool/request cannot
            # strand a browser after the manager has relinquished its refs.
            return await asyncio.shield(cleanup_task)
        except asyncio.CancelledError:
            # Preserve cancellation semantics, but first give the already
            # bounded teardown task a chance to finish and record whether the
            # profile lock was released. A repeated cancellation may interrupt
            # this await; the task remains stored for a later close() call.
            await asyncio.shield(cleanup_task)
            raise
        finally:
            if cleanup_task.done():
                self._cleanup_task = None

    async def _cleanup_resources(self, context: Any, playwright: Any) -> bool:
        """Run bounded teardown once and persist its trustworthy result."""
        teardown_complete = True

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
                teardown_complete = False
                logger.error(
                    "Timed out closing browser context after %ss",
                    _CLEANUP_TIMEOUT_SECONDS,
                )
            except Exception as exc:
                teardown_complete = False
                logger.error("Error closing browser context: %s", exc)

        if playwright is not None:
            try:
                await asyncio.wait_for(
                    playwright.stop(), timeout=_CLEANUP_TIMEOUT_SECONDS
                )
            except TimeoutError:
                teardown_complete = False
                logger.error(
                    "Timed out stopping playwright after %ss",
                    _CLEANUP_TIMEOUT_SECONDS,
                )
            except Exception as exc:
                teardown_complete = False
                logger.error("Error stopping playwright: %s", exc)

        logger.info("Browser closed")
        self._teardown_complete = teardown_complete
        return teardown_complete

    @property
    def page(self) -> Any:
        if not self._page:
            raise RuntimeError(
                "Browser not started. Use async context manager or call start()."
            )
        return self._page

    @property
    def teardown_complete(self) -> bool:
        """Whether this manager has confirmed release of its profile locks."""
        return self._teardown_complete

    @property
    def context(self) -> Any:
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

    @staticmethod
    def _is_linkedin_cookie_domain(cookie: Any) -> bool:
        domain = str(cookie.get("domain", "")).strip().lower().lstrip(".")
        return domain == "linkedin.com" or domain.endswith(".linkedin.com")

    @staticmethod
    def _has_nonempty_li_at(cookies: list[dict[str, Any]]) -> bool:
        """Return whether *cookies* contains a usable session-cookie value."""
        return any(
            cookie.get("name") == "li_at"
            and isinstance(cookie.get("value"), str)
            and bool(cookie["value"].strip())
            for cookie in cookies
        )

    async def export_cookies(self, cookie_path: str | Path | None = None) -> bool:
        """Export LinkedIn cookies to a portable JSON file."""
        if not self._context:
            logger.warning("Cannot export cookies: no browser context")
            return False

        path = Path(cookie_path) if cookie_path else self._default_cookie_path()
        try:
            all_cookies = await self._context.cookies()
            cookies = [
                self._normalize_cookie_domain(c)
                for c in all_cookies
                if self._is_linkedin_cookie_domain(c)
            ]
            if not self._has_nonempty_li_at(cookies):
                logger.warning(
                    "Refusing to export cookies to %s: no non-empty li_at cookie; "
                    "existing snapshot preserved",
                    path,
                )
                return False
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
            # indexed_db was originally a Patchright-only storage_state()
            # extension; only pass it when the configured engine's adapter
            # declares support, rather than assuming every engine/version
            # combination behaves the same way.
            storage_state_kwargs: dict[str, Any] = {"path": storage_path}
            if ENGINES[self.engine].supports_indexed_db:
                storage_state_kwargs["indexed_db"] = indexed_db
            await self._context.storage_state(**storage_state_kwargs)
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
        a fresh profile. Missing or semantically invalid snapshots return
        ``False``. Once a valid subset reaches the browser driver, injection
        failures are infrastructure failures and raise :class:`NetworkError`;
        callers must not misreport a dead driver as rejected authentication.
        """
        if not self._context:
            raise NetworkError("Cannot import cookies: no browser context")

        path = Path(cookie_path) if cookie_path else self._default_cookie_path()
        if not path.exists():
            logger.debug("No portable cookie file at %s", path)
            return False

        try:
            raw_snapshot = path.read_text()
        except UnicodeDecodeError:
            logger.warning("Portable cookie snapshot is not valid UTF-8: %s", path)
            return False
        except OSError as exc:
            raise NetworkError(f"Could not read portable cookies from {path}") from exc

        try:
            all_cookies = json.loads(raw_snapshot)
        except json.JSONDecodeError:
            logger.warning("Portable cookie snapshot is not valid JSON: %s", path)
            return False
        if not isinstance(all_cookies, list) or not all_cookies:
            logger.debug("Cookie snapshot is empty or not a JSON list: %s", path)
            return False
        if not all(isinstance(cookie, dict) for cookie in all_cookies):
            logger.warning("Portable cookie snapshot has invalid entries: %s", path)
            return False

        try:
            resolved_preset_name, bridge_cookie_names = self._bridge_cookie_names(
                preset_name
            )
            cookies = [
                self._normalize_cookie_domain(cookie)
                for cookie in all_cookies
                if self._is_linkedin_cookie_domain(cookie)
                and cookie.get("name") in bridge_cookie_names
            ]
        except (AttributeError, TypeError, ValueError):
            logger.warning("Portable cookie snapshot is semantically invalid: %s", path)
            return False

        has_li_at = self._has_nonempty_li_at(cookies)
        if not has_li_at:
            logger.warning("No non-empty li_at cookie found in %s", path)
            return False

        try:
            await self._context.add_cookies(cookies)
        except Exception as exc:
            raise NetworkError(
                f"Browser driver failed while injecting cookies from {path}: {exc}"
            ) from exc
        logger.info(
            "Imported %d LinkedIn bridge cookies from %s (preset=%s, li_at=%s): %s",
            len(cookies),
            path,
            resolved_preset_name,
            has_li_at,
            ", ".join(str(cookie["name"]) for cookie in cookies),
        )
        return True

    @staticmethod
    def _cookie_snapshot_key(cookie: dict[str, Any]) -> tuple[str, str, str]:
        """Return the identity Playwright uses when replacing a cookie."""
        return (
            str(cookie.get("name", "")),
            str(cookie.get("domain", "")).strip().lower(),
            str(cookie.get("path", "/")),
        )

    async def refresh_imported_cookie_snapshot(
        self,
        cookie_path: str | Path,
        *,
        preset_name: str | None = None,
    ) -> bool:
        """Merge post-validation cookies into a full imported snapshot.

        Browser import injects only a bridge subset into an isolated profile,
        while ``cookie_path`` contains every LinkedIn cookie extracted from the
        source browser. After ``/feed/`` succeeds, LinkedIn may rotate ``li_at``
        or ``JSESSIONID`` -- or delete one of the injected tokens. Preserve the
        non-injected full-set cookies, replace them with every LinkedIn cookie
        observed after validation, and never restore an injected cookie that is
        no longer present in the browser context.

        ``preset_name=None`` resolves through the same environment-aware
        default as :meth:`import_cookies`, so runtime refresh never treats a
        cookie it did not inject as server-deleted.

        The staged snapshot is changed only when the post-validation context
        still has a non-empty ``li_at``. Any read/context/write failure leaves
        the existing snapshot untouched and returns ``False``.
        """
        if not self._context:
            logger.warning("Cannot refresh imported cookies: no browser context")
            return False

        path = Path(cookie_path)
        try:
            original_payload = json.loads(path.read_text())
            if not isinstance(original_payload, list):
                raise ValueError("cookie snapshot must contain a JSON list")

            original = [
                self._normalize_cookie_domain(cookie)
                for cookie in original_payload
                if isinstance(cookie, dict) and self._is_linkedin_cookie_domain(cookie)
            ]
            _resolved_name, injected_names = self._bridge_cookie_names(preset_name)
            observed = [
                self._normalize_cookie_domain(cookie)
                for cookie in await self._context.cookies()
                if self._is_linkedin_cookie_domain(cookie)
            ]
            if not self._has_nonempty_li_at(observed):
                logger.warning(
                    "Refusing to refresh imported cookies at %s: the "
                    "post-validation context has no non-empty li_at",
                    path,
                )
                return False

            # Every bridge-named cookie in the original snapshot was injected.
            # Omit that whole set first, then add back only what survived (or
            # was rotated) in the validated browser. This is what prevents a
            # server-deleted token from being resurrected by the full snapshot.
            merged = {
                self._cookie_snapshot_key(cookie): cookie
                for cookie in original
                if cookie.get("name") not in injected_names
            }
            for cookie in observed:
                merged[self._cookie_snapshot_key(cookie)] = cookie
            refreshed = list(merged.values())
            if not self._has_nonempty_li_at(refreshed):
                return False

            secure_mkdir(path.parent)
            harden_linkedin_tree(path.parent)
            secure_write_text(
                path, json.dumps(refreshed, indent=2), mode=_PRIVATE_FILE_MODE
            )
            logger.info(
                "Refreshed imported snapshot with %d post-validation cookies",
                len(observed),
            )
            return True
        except Exception:
            logger.exception("Failed to refresh imported cookies at %s", path)
            return False

    def cookie_file_exists(self, cookie_path: str | Path | None = None) -> bool:
        """Check if a portable cookie file exists."""
        path = Path(cookie_path) if cookie_path else self._default_cookie_path()
        return path.exists()
