"""Browser lifecycle management using Patchright with persistent context."""

import json
import logging
import os
from pathlib import Path
from typing import Any

from patchright.async_api import (
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)

from linkedin_mcp_server.common_utils import secure_mkdir, secure_write_text

from .exceptions import NetworkError

logger = logging.getLogger(__name__)

_DEFAULT_USER_DATA_DIR = Path.home() / ".linkedin-mcp" / "profile"


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
        user_agent: str | None = None,
        **launch_options: Any,
    ):
        self.user_data_dir = str(Path(user_data_dir).expanduser())
        self.headless = headless
        self.slow_mo = slow_mo
        self.viewport = viewport or {"width": 1280, "height": 720}
        self.user_agent = user_agent
        self.launch_options = launch_options

        self._playwright: Playwright | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._is_authenticated = False

    async def __aenter__(self) -> "BrowserManager":
        await self.start()
        return self

    async def __aexit__(
        self, exc_type: object, exc_val: object, exc_tb: object
    ) -> None:
        await self.close()

    async def start(self) -> None:
        """Start Patchright and launch persistent browser context."""
        if self._context is not None:
            raise RuntimeError("Browser already started. Call close() first.")
        try:
            self._playwright = await async_playwright().start()

            secure_mkdir(Path(self.user_data_dir))

            context_options: dict[str, Any] = {
                "headless": self.headless,
                "slow_mo": self.slow_mo,
                "viewport": self.viewport,
                **self.launch_options,
                "locale": "en-US",
            }

            if self.user_agent:
                context_options["user_agent"] = self.user_agent

            self._context = await self._playwright.chromium.launch_persistent_context(
                self.user_data_dir,
                **context_options,
            )

            logger.info(
                "Persistent browser launched (headless=%s, user_data_dir=%s)",
                self.headless,
                self.user_data_dir,
            )

            if self._context.pages:
                self._page = self._context.pages[0]
            else:
                self._page = await self._context.new_page()

            logger.info("Browser context and page ready")

        except Exception as e:
            await self.close()
            raise NetworkError(f"Failed to start browser: {e}") from e

    async def close(self) -> None:
        """Close persistent context and cleanup resources."""
        context = self._context
        playwright = self._playwright
        self._context = None
        self._page = None
        self._playwright = None

        if context is None and playwright is None:
            return

        if context is not None:
            try:
                await context.close()
            except Exception as exc:
                logger.error("Error closing browser context: %s", exc)

        if playwright is not None:
            try:
                await playwright.stop()
            except Exception as exc:
                logger.error("Error stopping playwright: %s", exc)

        logger.info("Browser closed")

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
            all_cookies = await self._context.cookies()
            cookies = [
                self._normalize_cookie_domain(c)
                for c in all_cookies
                if "linkedin.com" in c.get("domain", "")
            ]
            secure_write_text(path, json.dumps(cookies, indent=2))
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
        try:
            await self._context.storage_state(
                path=storage_path,
                indexed_db=indexed_db,
            )
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

            await self._context.add_cookies(cookies)  # type: ignore[arg-type]
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
