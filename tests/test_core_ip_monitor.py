"""Tests for outbound-IP drift monitoring (core.ip_monitor)."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from linkedin_mcp_server.core.ip_monitor import (
    IpDriftMonitor,
    get_ip_drift_monitor,
    reset_ip_drift_monitor_for_testing,
)


@pytest.fixture(autouse=True)
def _reset_singleton():
    reset_ip_drift_monitor_for_testing()
    yield
    reset_ip_drift_monitor_for_testing()


def _make_page(ip_sequence):
    page = MagicMock()
    page.evaluate = AsyncMock(side_effect=ip_sequence)
    return page


class TestEstablishBaseline:
    async def test_stores_the_fetched_ip(self):
        monitor = IpDriftMonitor()
        page = _make_page(["1.2.3.4"])
        result = await monitor.establish_baseline(page)
        assert result == "1.2.3.4"
        assert monitor.baseline_ip == "1.2.3.4"

    async def test_handles_a_failed_fetch(self):
        monitor = IpDriftMonitor()
        page = _make_page([None])
        result = await monitor.establish_baseline(page)
        assert result is None
        assert monitor.baseline_ip is None


class TestCheck:
    async def test_no_baseline_means_no_drift(self):
        monitor = IpDriftMonitor()
        page = _make_page(["1.2.3.4"])
        assert await monitor.check(page) is True
        page.evaluate.assert_not_called()  # never fetches without a baseline

    async def test_same_ip_is_not_drift(self):
        monitor = IpDriftMonitor()
        page = _make_page(["1.2.3.4", "1.2.3.4"])
        await monitor.establish_baseline(page)
        assert await monitor.check(page) is True

    async def test_different_ip_is_drift(self):
        monitor = IpDriftMonitor()
        page = _make_page(["1.2.3.4", "5.6.7.8"])
        await monitor.establish_baseline(page)
        assert await monitor.check(page) is False

    async def test_failed_current_fetch_is_not_treated_as_drift(self):
        monitor = IpDriftMonitor()
        page = _make_page(["1.2.3.4", None])
        await monitor.establish_baseline(page)
        assert await monitor.check(page) is True

    async def test_evaluate_exception_is_treated_as_inconclusive(self):
        monitor = IpDriftMonitor()
        page = MagicMock()
        page.evaluate = AsyncMock(side_effect=["1.2.3.4", RuntimeError("network gone")])
        await monitor.establish_baseline(page)
        assert await monitor.check(page) is True


class TestSingleton:
    def test_get_ip_drift_monitor_returns_same_instance(self):
        assert get_ip_drift_monitor() is get_ip_drift_monitor()

    def test_reset_creates_a_fresh_instance(self):
        first = get_ip_drift_monitor()
        reset_ip_drift_monitor_for_testing()
        assert get_ip_drift_monitor() is not first
