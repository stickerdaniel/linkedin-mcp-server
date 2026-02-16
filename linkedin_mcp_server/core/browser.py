"""Browser lifecycle management using Patchright with persistent context."""

import json
import logging
from pathlib import Path
from typing import Any

from patchright.async_api import (
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)

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

            Path(self.user_data_dir).mkdir(parents=True, exist_ok=True)

            context_options: dict[str, Any] = {
                "headless": self.headless,
                "slow_mo": self.slow_mo,
                "viewport": self.viewport,
                **self.launch_options,
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
        try:
            if self._context:
                await self._context.close()
                self._context = None
                self._page = None

            if self._playwright:
                await self._playwright.stop()
                self._playwright = None

            logger.info("Browser closed")

        except Exception as e:
            logger.error("Error closing browser: %s", e)

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
            path.write_text(json.dumps(cookies, indent=2))
            logger.info("Exported %d LinkedIn cookies to %s", len(cookies), path)
            return True
        except Exception:
            logger.exception("Failed to export cookies")
            return False

    _AUTH_COOKIE_NAMES = frozenset({"li_at", "li_rm"})

    async def import_cookies(self, cookie_path: str | Path | None = None) -> bool:
        """Import auth cookies (li_at, li_rm) from a portable JSON file."""
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

            cookies = [
                self._normalize_cookie_domain(c)
                for c in all_cookies
                if c.get("name") in self._AUTH_COOKIE_NAMES
            ]
            if not cookies:
                logger.warning("No auth cookies (li_at/li_rm) found in %s", path)
                return False

            # Clear undecryptable cookies from the persistent store first.
            await self._context.clear_cookies()
            await self._context.add_cookies(cookies)  # type: ignore[arg-type]
            logger.info(
                "Imported %d auth cookies from %s: %s",
                len(cookies),
                path,
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
