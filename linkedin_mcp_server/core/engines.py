"""Browser engine adapters: one launch/profile-path/install implementation
per engine, registered in ``ENGINES``.

Before this module existed, engine selection was ``if self.engine ==
"camoufox": ... else: ...`` hand-duplicated across core/browser.py (launch),
core/utils.py (TIMEOUT_ERRORS), and bootstrap.py (install-skip logic) -- a
cross-cutting concern copied per call site. One of those copies (the shared
``user_data_dir`` blast radius a destructive reset could hit) was missed on
the first pass despite careful review, and caused real data loss. Adding a
third engine now means writing one adapter class and registering it here,
not repeating that grep-every-call-site exercise.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
import logging
import os
from pathlib import Path
from typing import Any, Protocol, cast

from .camoufox_identity import prepare_camoufox_launch_options

logger = logging.getLogger(__name__)

_PLAYWRIGHT_STOP_TIMEOUT_SECONDS = 10
_LAUNCH_TEARDOWN_COMPLETE_ATTR = "_linkedin_mcp_launch_teardown_complete"


def launch_teardown_was_confirmed(exc: BaseException) -> bool:
    """Return whether an adapter released resources after a failed launch."""
    return bool(getattr(exc, _LAUNCH_TEARDOWN_COMPLETE_ATTR, True))


async def _stop_playwright_after_failed_launch(playwright: Any, *, engine: str) -> bool:
    """Best-effort driver teardown that never masks a launch failure.

    The adapter has not returned yet, so ``BrowserManager`` cannot see the
    locally-started Playwright instance and cannot stop it itself. Bound this
    cleanup because a broken driver connection can make ``stop()`` hang too.
    """
    try:
        await asyncio.wait_for(
            playwright.stop(), timeout=_PLAYWRIGHT_STOP_TIMEOUT_SECONDS
        )
        return True
    except TimeoutError:
        logger.error(
            "Timed out stopping Playwright after failed %s launch after %ss",
            engine,
            _PLAYWRIGHT_STOP_TIMEOUT_SECONDS,
        )
        return False
    except asyncio.CancelledError:
        logger.error("Playwright teardown was cancelled after failed %s launch", engine)
        return False
    except Exception as exc:
        logger.error(
            "Error stopping Playwright after failed %s launch: %s", engine, exc
        )
        return False


class EngineAdapter(Protocol):
    """One browser engine's launch, profile-path, and install-skip logic."""

    name: str
    supports_indexed_db: bool

    @property
    def timeout_error_classes(self) -> tuple[type[Exception], ...]:
        """Exception class(es) this engine's driver raises on a timeout.

        Different engines can be different pip packages (Patchright vs.
        vanilla Playwright) with structurally-identical but distinct
        TimeoutError classes -- callers must catch the union
        (``core.utils.TIMEOUT_ERRORS``), never just one engine's class.
        """
        ...

    def profile_dir(self, user_data_dir: str | Path) -> Path:
        """Return the actual on-disk profile directory this engine launches
        into. The single source of truth for that mapping -- both the
        launch path and any cleanup/reset code must resolve through this so
        they can never disagree about which directory an engine owns."""
        ...

    async def launch(
        self,
        *,
        user_data_dir: str | Path,
        headless: bool,
        slow_mo: int,
        viewport: dict[str, int],
        user_agent: str | None,
        launch_options: dict[str, Any],
        camoufox_identity_path: Path | None,
        expected_camoufox_identity_sha256: str | None,
    ) -> tuple[Any, Any]:
        """Start this engine's driver and launch a persistent context.

        Returns ``(playwright_instance, context)``.
        """
        ...

    def needs_managed_install(self, chrome_path: str | None) -> bool:
        """Whether bootstrap.py's Patchright binary-install state machine
        applies when this engine is selected. ``False`` means the engine
        manages its own binary (or an operator-supplied executable bypasses
        the managed one), so the background installer is a no-op."""
        ...


class PatchrightAdapter:
    """Chromium via Patchright (the default engine)."""

    name = "patchright"
    supports_indexed_db = True

    @property
    def timeout_error_classes(self) -> tuple[type[Exception], ...]:
        from patchright.async_api import TimeoutError as PatchrightTimeoutError

        return (PatchrightTimeoutError,)

    def profile_dir(self, user_data_dir: str | Path) -> Path:
        return Path(user_data_dir).expanduser()

    async def launch(
        self,
        *,
        user_data_dir: str | Path,
        headless: bool,
        slow_mo: int,
        viewport: dict[str, int],
        user_agent: str | None,
        launch_options: dict[str, Any],
        camoufox_identity_path: Path | None,
        expected_camoufox_identity_sha256: str | None,
    ) -> tuple[Any, Any]:
        from patchright.async_api import async_playwright

        playwright = await async_playwright().start()
        try:
            context_options: dict[str, Any] = {
                "headless": headless,
                "slow_mo": slow_mo,
                "viewport": viewport,
                **launch_options,
                "locale": "en-US",
            }
            if user_agent:
                context_options["user_agent"] = user_agent

            context = await playwright.chromium.launch_persistent_context(
                str(self.profile_dir(user_data_dir)),
                **context_options,
            )
        except (Exception, asyncio.CancelledError) as exc:
            teardown_complete = await _stop_playwright_after_failed_launch(
                playwright, engine=self.name
            )
            if not teardown_complete:
                setattr(exc, _LAUNCH_TEARDOWN_COMPLETE_ATTR, False)
            raise
        return playwright, context

    def needs_managed_install(self, chrome_path: str | None) -> bool:
        # A custom executable bypasses the managed binary entirely.
        return not chrome_path


class CamoufoxAdapter:
    """Firefox via Camoufox -- a stealth-patched build driven through
    vanilla Playwright (a different pip package from Patchright)."""

    name = "camoufox"
    supports_indexed_db = False
    _PROFILE_SUBDIR = "camoufox"

    @property
    def timeout_error_classes(self) -> tuple[type[Exception], ...]:
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError

        return (PlaywrightTimeoutError,)

    def profile_dir(self, user_data_dir: str | Path) -> Path:
        # Firefox's persistent-profile layout is not Chromium's; namespace
        # into a subdirectory so a shared user_data_dir never interleaves
        # Camoufox's profile files with a Patchright/Chromium one (see the
        # profile-wipe incident this module's docstring references).
        return Path(user_data_dir).expanduser() / self._PROFILE_SUBDIR

    async def launch(
        self,
        *,
        user_data_dir: str | Path,
        headless: bool,
        slow_mo: int,
        viewport: dict[str, int],
        user_agent: str | None,
        launch_options: dict[str, Any],
        camoufox_identity_path: Path | None,
        expected_camoufox_identity_sha256: str | None,
    ) -> tuple[Any, Any]:
        from camoufox.async_api import AsyncNewBrowser
        from camoufox.utils import launch_options as camoufox_launch_options
        from playwright.async_api import async_playwright as pw_async_playwright

        if camoufox_identity_path is None:
            raise RuntimeError(
                "Camoufox requires a persistent identity path; run the normal "
                "login/import/session flow"
            )

        playwright = await pw_async_playwright().start()
        try:
            camoufox_options: dict[str, Any] = {
                "os": "linux",
                "humanize": True,
                "geoip": True,
                "slow_mo": slow_mo,
                "viewport": viewport,
                **launch_options,
            }
            if user_agent:
                camoufox_options["user_agent"] = user_agent

            # Camoufox merges the process environment *after* its generated
            # CAMOU_CONFIG values. Strip inherited chunks before asking it to
            # generate the current launch options so a stale/malicious parent
            # environment can never become the persisted identity. All other
            # environment values remain current and are never written to disk.
            configured_env = camoufox_options.get("env")
            if configured_env is None:
                configured_env = os.environ
            if not isinstance(configured_env, Mapping):
                raise TypeError("Camoufox env launch option must be a mapping")
            camoufox_options["env"] = {
                key: value
                for key, value in configured_env.items()
                if not (isinstance(key, str) and key.startswith("CAMOU_CONFIG_"))
            }
            generate_options = cast(Any, camoufox_launch_options)
            current_options = await asyncio.to_thread(
                generate_options,
                headless=headless,
                user_data_dir=str(self.profile_dir(user_data_dir)),
                **camoufox_options,
            )
            prepared_options, _identity_sha256 = await prepare_camoufox_launch_options(
                current_options,
                camoufox_identity_path,
                expected_identity_sha256=expected_camoufox_identity_sha256,
            )
            context = await AsyncNewBrowser(
                playwright,
                persistent_context=True,
                from_options=prepared_options,
            )
        except (Exception, asyncio.CancelledError) as exc:
            teardown_complete = await _stop_playwright_after_failed_launch(
                playwright, engine=self.name
            )
            if not teardown_complete:
                setattr(exc, _LAUNCH_TEARDOWN_COMPLETE_ATTR, False)
            raise
        return playwright, context

    def needs_managed_install(self, chrome_path: str | None) -> bool:
        # Camoufox manages its own Firefox binary (`camoufox fetch`, cached
        # under ~/.cache/camoufox) entirely outside the Patchright
        # install/version-tracking state machine.
        return False


ENGINES: dict[str, EngineAdapter] = {
    "patchright": PatchrightAdapter(),
    "camoufox": CamoufoxAdapter(),
}
