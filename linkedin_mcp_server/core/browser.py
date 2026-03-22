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
        channel: str | None = "chrome",
        locale: str = "en-US",
        timezone_id: str = "America/Sao_Paulo",
        accept_language: str = "en-US,en;q=0.9",
        **launch_options: Any,
    ):
        self.user_data_dir = str(Path(user_data_dir).expanduser())
        self.headless = headless
        self.slow_mo = slow_mo
        self.viewport = viewport or {"width": 1280, "height": 720}
        self.user_agent = user_agent
        self.channel = channel
        self.locale = locale
        self.timezone_id = timezone_id
        self.accept_language = accept_language
        self.launch_options = launch_options

        self._playwright: Playwright | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._is_authenticated = False

    def _build_context_options(self) -> dict[str, Any]:
        """Build shared context options dict for both persistent and temp contexts."""
        options: dict[str, Any] = {
            "viewport": self.viewport,
            "locale": self.locale,
            "timezone_id": self.timezone_id,
        }
        if self.channel:
            options["channel"] = self.channel
        if self.user_agent:
            options["user_agent"] = self.user_agent
        return options

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

            context_options = self._build_context_options()
            context_options["headless"] = self.headless
            context_options["slow_mo"] = self.slow_mo
            context_options.update(self.launch_options)

            # Stealth browser args (extensible list)
            existing_args = list(context_options.get("args", []))
            existing_args.append("--disable-blink-features=AutomationControlled")
            existing_args.append("--disable-async-dns")
            # Required for Chrome in Docker containers (sandbox blocks DNS/networking)
            is_docker = os.environ.get("container") or Path("/.dockerenv").exists()
            if is_docker:
                existing_args.extend([
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-setuid-sandbox",
                ])
                context_options["chromium_sandbox"] = False
            context_options["args"] = existing_args

            self._context = await self._playwright.chromium.launch_persistent_context(
                self.user_data_dir,
                **context_options,
            )

            # Set extra HTTP headers (not a context option — separate API call)
            await self._context.set_extra_http_headers(
                {"Accept-Language": self.accept_language}
            )

            # Apply stealth init scripts before any navigation.
            # IMPORTANT: context.add_init_script breaks DNS resolution when
            # using channel="chrome" (Chrome real) inside Docker containers.
            # Patchright already patches the critical signals (webdriver, etc.)
            # natively, so we skip init scripts in Docker and rely on Patchright.
            if not is_docker:
                from .stealth import get_stealth_init_scripts

                for script in get_stealth_init_scripts():
                    await self._context.add_init_script(script)

            logger.info(
                "Persistent browser launched (headless=%s, user_data_dir=%s, locale=%s, tz=%s)",
                self.headless,
                self.user_data_dir,
                self.locale,
                self.timezone_id,
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
            path.write_text(json.dumps(cookies, indent=2))
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
        storage_path.parent.mkdir(parents=True, exist_ok=True)
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

    async def import_storage_state(self, path: str | Path) -> bool:
        """Import LinkedIn cookies from a Playwright storage-state snapshot."""
        if not self._context:
            logger.warning("Cannot import storage state: no browser context")
            return False

        storage_path = Path(path)
        if not storage_path.exists():
            logger.debug("No source storage-state file at %s", storage_path)
            return False

        try:
            payload = json.loads(storage_path.read_text())
            all_cookies = payload.get("cookies") or []
            cookies = [
                self._normalize_cookie_domain(c)
                for c in all_cookies
                if "linkedin.com" in c.get("domain", "")
            ]

            has_li_at = any(c.get("name") == "li_at" for c in cookies)
            if not has_li_at:
                logger.warning("No li_at cookie found in storage-state %s", path)
                return False

            await self._context.add_cookies(cookies)  # type: ignore[arg-type]
            logger.info(
                "Imported %d LinkedIn cookies from storage-state %s (li_at=%s): %s",
                len(cookies),
                storage_path,
                has_li_at,
                ", ".join(c["name"] for c in cookies),
            )
            return True
        except Exception:
            logger.exception("Failed to import storage state from %s", storage_path)
            return False

    async def materialize_storage_state_auth(self, path: str | Path) -> bool:
        """Warm an authenticated Linux-native cookie jar from source storage state.

        LinkedIn accepts source ``storage_state`` reliably when it is applied as
        part of a fresh non-persistent context creation. The warmed cookies from
        that context can then be copied into the persistent runtime profile.
        """
        if not self._context or not self._playwright:
            logger.warning(
                "Cannot materialize storage state auth: browser context not ready"
            )
            return False

        storage_path = Path(path)
        if not storage_path.exists():
            logger.debug("No source storage-state file at %s", storage_path)
            return False

        try:
            payload = json.loads(storage_path.read_text())
            source_cookies = payload.get("cookies") or []
            has_li_at = any(
                c.get("name") == "li_at" and "linkedin.com" in c.get("domain", "")
                for c in source_cookies
            )
            if not has_li_at:
                logger.warning("No li_at cookie found in storage-state %s", path)
                return False
        except Exception:
            logger.exception("Failed to read storage state from %s", storage_path)
            return False

        temp_browser = None
        temp_context = None
        try:
            launch_opts = {**self.launch_options}
            if self.channel:
                launch_opts["channel"] = self.channel
            temp_browser = await self._playwright.chromium.launch(**launch_opts)
            temp_ctx_options = self._build_context_options()
            # Remove channel — it's a launch option, not a context option for new_context
            temp_ctx_options.pop("channel", None)
            temp_ctx_options["storage_state"] = storage_path
            temp_context = await temp_browser.new_context(**temp_ctx_options)
            await temp_context.set_extra_http_headers(
                {"Accept-Language": self.accept_language}
            )
            page = await temp_context.new_page()
            await page.goto(
                "https://www.linkedin.com/feed/",
                wait_until="domcontentloaded",
                timeout=15000,
            )
            cookies = [
                self._normalize_cookie_domain(c)
                for c in await temp_context.cookies()
                if "linkedin.com" in c.get("domain", "")
            ]
            has_li_at = any(c.get("name") == "li_at" for c in cookies)
            if not has_li_at:
                logger.warning(
                    "No li_at cookie remained after warming storage-state %s",
                    storage_path,
                )
                return False
            await self._context.add_cookies(cookies)  # type: ignore[arg-type]
            logger.info(
                "Materialized %d LinkedIn cookies from source storage-state %s "
                "into persistent context",
                len(cookies),
                storage_path,
            )
            return True
        except Exception:
            logger.exception(
                "Failed to materialize persistent auth from storage-state %s",
                storage_path,
            )
            return False
        finally:
            if temp_context is not None:
                try:
                    await temp_context.close()
                except Exception:
                    logger.debug("Failed to close temporary warm-up context")
            if temp_browser is not None:
                try:
                    await temp_browser.close()
                except Exception:
                    logger.debug("Failed to close temporary warm-up browser")

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
