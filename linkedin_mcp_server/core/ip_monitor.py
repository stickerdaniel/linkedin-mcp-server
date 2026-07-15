"""Outbound-IP drift monitoring for proxy-configured sessions.

A mid-session outbound-IP change on an authenticated LinkedIn session is a
strong risk signal -- the account now looks like it's being accessed from
a different network than it logged in from, which is exactly the kind of
anomaly LinkedIn's own risk scoring watches for. This module fetches the
outbound IP once as a baseline, then a cheap periodic check compares
against it and logs a WARNING on drift.

Deliberately scoped to monitoring only -- no pool, no rotation. A single
static proxy per session is today's design; this just makes silent drift
of that single proxy's exit IP visible instead of invisible.

The IP must be fetched from *inside* the browser page (page.evaluate),
not via a plain Python HTTP client -- the configured proxy is a
browser/Playwright-level setting, so a request made from this process's
own network stack would bypass it entirely and report the host's IP, not
the session's actual egress IP.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_IP_CHECK_SCRIPT = """
async () => {
    try {
        const res = await fetch('https://api.ipify.org?format=json');
        const data = await res.json();
        return data.ip || null;
    } catch (e) {
        return null;
    }
}
"""


class IpDriftMonitor:
    """Tracks a session's outbound IP and flags mid-session drift.

    Only meaningful when a proxy is configured -- without one there's
    nothing to "drift" against. Callers should gate construction/use on
    ``config.browser.proxy_settings()`` being set.
    """

    def __init__(self) -> None:
        self._baseline_ip: str | None = None

    @property
    def baseline_ip(self) -> str | None:
        return self._baseline_ip

    async def establish_baseline(self, page: Any) -> str | None:
        """Fetch and store the current outbound IP as the session baseline."""
        ip = await self._fetch_ip(page)
        self._baseline_ip = ip
        if ip:
            logger.info("Established outbound IP baseline: %s", ip)
        else:
            logger.warning(
                "Could not establish outbound IP baseline; drift checks will "
                "be skipped for this session"
            )
        return ip

    async def check(self, page: Any) -> bool:
        """Return False if the current IP has drifted from the baseline.

        Returns True (no drift / inconclusive) if no baseline was ever
        established or the current fetch fails -- a failed fetch is not
        itself evidence of drift.
        """
        if self._baseline_ip is None:
            return True
        current = await self._fetch_ip(page)
        if current is None:
            logger.debug("Could not fetch current outbound IP for drift check")
            return True
        if current != self._baseline_ip:
            logger.warning(
                "Outbound IP drift detected: baseline=%s current=%s -- session "
                "may now be exiting from a different location than it "
                "authenticated from",
                self._baseline_ip,
                current,
            )
            return False
        return True

    @staticmethod
    async def _fetch_ip(page: Any) -> str | None:
        try:
            result = await page.evaluate(_IP_CHECK_SCRIPT)
        except Exception:
            logger.debug("IP fetch via page.evaluate failed", exc_info=True)
            return None
        return result if isinstance(result, str) and result else None


_default_monitor: IpDriftMonitor | None = None


def get_ip_drift_monitor() -> IpDriftMonitor:
    """Return the process-wide IP drift monitor singleton."""
    global _default_monitor
    if _default_monitor is None:
        _default_monitor = IpDriftMonitor()
    return _default_monitor


def reset_ip_drift_monitor_for_testing() -> None:
    global _default_monitor
    _default_monitor = None
