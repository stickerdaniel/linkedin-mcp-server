from unittest.mock import AsyncMock, MagicMock

import mcp.types as mt
import pytest
from fastmcp.tools import ToolResult

from linkedin_mcp_server import bootstrap, update_check
from linkedin_mcp_server.update_check import (
    UpdateNoticeMiddleware,
    _check_disabled,
    pending_update_notice,
    prime_from_cache,
    refresh_latest_version,
)


@pytest.fixture(autouse=True)
def _not_source(monkeypatch):
    # Keep tests deterministic regardless of how the package under test is
    # installed; a source/editable checkout would otherwise disable the network path.
    monkeypatch.setattr(update_check, "_is_source_install", lambda: False)


class TestPendingUpdateNotice:
    def test_notice_managed_points_at_uvx_config(self, monkeypatch):
        monkeypatch.setattr(update_check, "__version__", "4.16.1")
        monkeypatch.setattr(update_check, "_latest_known", "4.18.0")
        monkeypatch.setattr(
            bootstrap, "get_runtime_policy", lambda: bootstrap.RuntimePolicy.MANAGED
        )
        monkeypatch.delenv("LINKEDIN_MCP_RUNTIME", raising=False)

        notice = pending_update_notice()

        assert notice is not None
        assert "4.18.0" in notice
        assert "4.16.1" in notice
        assert "uvx mcp-server-linkedin@latest" in notice
        assert ".mcpb" not in notice

    def test_notice_for_docker_targets_the_image(self, monkeypatch):
        monkeypatch.setattr(update_check, "__version__", "4.16.1")
        monkeypatch.setattr(update_check, "_latest_known", "4.18.0")
        monkeypatch.setattr(
            bootstrap, "get_runtime_policy", lambda: bootstrap.RuntimePolicy.DOCKER
        )

        notice = pending_update_notice()

        assert notice is not None
        assert "Docker" in notice
        assert "uvx" not in notice

    def test_notice_for_mcpb_links_latest_release(self, monkeypatch):
        monkeypatch.setattr(update_check, "__version__", "4.16.1")
        monkeypatch.setattr(update_check, "_latest_known", "4.18.0")
        monkeypatch.setattr(
            bootstrap, "get_runtime_policy", lambda: bootstrap.RuntimePolicy.MANAGED
        )
        monkeypatch.setenv("LINKEDIN_MCP_RUNTIME", "mcpb")

        notice = pending_update_notice()

        assert notice is not None
        assert "releases/latest" in notice
        assert ".mcpb" in notice
        assert "uvx" not in notice

    def test_notice_on_two_patches_behind(self, monkeypatch):
        monkeypatch.setattr(update_check, "__version__", "4.16.1")
        monkeypatch.setattr(update_check, "_latest_known", "4.16.3")

        assert pending_update_notice() is not None

    def test_no_notice_for_single_patch(self, monkeypatch):
        monkeypatch.setattr(update_check, "__version__", "4.16.1")
        monkeypatch.setattr(update_check, "_latest_known", "4.16.2")

        assert pending_update_notice() is None

    def test_no_notice_when_current(self, monkeypatch):
        monkeypatch.setattr(update_check, "__version__", "4.16.1")
        monkeypatch.setattr(update_check, "_latest_known", "4.16.1")

        assert pending_update_notice() is None

    def test_no_notice_for_prerelease_latest(self, monkeypatch):
        monkeypatch.setattr(update_check, "__version__", "4.16.1")
        monkeypatch.setattr(update_check, "_latest_known", "4.18.0rc1")

        assert pending_update_notice() is None

    def test_no_notice_for_dev_install(self, monkeypatch):
        monkeypatch.setattr(update_check, "__version__", "0.0.0.dev")
        monkeypatch.setattr(update_check, "_latest_known", "9.9.9")

        assert pending_update_notice() is None

    def test_no_notice_when_latest_unknown(self, monkeypatch):
        monkeypatch.setattr(update_check, "__version__", "4.16.1")
        monkeypatch.setattr(update_check, "_latest_known", None)

        assert pending_update_notice() is None


class TestRefreshLatestVersion:
    async def test_disabled_skips_network(self, monkeypatch):
        monkeypatch.setenv("LINKEDIN_MCP_CHECK_FOR_UPDATES", "off")
        monkeypatch.setattr(update_check, "_latest_known", None)
        fetch = MagicMock()
        monkeypatch.setattr(update_check, "_fetch_latest_from_pypi", fetch)

        await refresh_latest_version()

        fetch.assert_not_called()
        assert update_check._latest_known is None

    async def test_pep440_dev_release_does_not_poll(self, monkeypatch):
        # A real installed dev build, not just the 0.0.0.dev fallback.
        monkeypatch.delenv("LINKEDIN_MCP_CHECK_FOR_UPDATES", raising=False)
        monkeypatch.delenv("CI", raising=False)
        monkeypatch.setattr(update_check, "__version__", "4.17.0.dev1")
        monkeypatch.setattr(update_check, "_latest_known", None)
        fetch = MagicMock()
        monkeypatch.setattr(update_check, "_fetch_latest_from_pypi", fetch)

        assert _check_disabled() is True
        await refresh_latest_version()

        fetch.assert_not_called()
        assert update_check._latest_known is None

    async def test_source_install_does_not_poll(self, monkeypatch):
        monkeypatch.delenv("LINKEDIN_MCP_CHECK_FOR_UPDATES", raising=False)
        monkeypatch.delenv("CI", raising=False)
        monkeypatch.setattr(update_check, "__version__", "4.16.1")
        monkeypatch.setattr(update_check, "_is_source_install", lambda: True)
        monkeypatch.setattr(update_check, "_latest_known", None)
        fetch = MagicMock()
        monkeypatch.setattr(update_check, "_fetch_latest_from_pypi", fetch)

        assert _check_disabled() is True
        await refresh_latest_version()

        fetch.assert_not_called()
        assert update_check._latest_known is None

    async def test_fetches_and_caches_when_stale(self, monkeypatch):
        monkeypatch.delenv("LINKEDIN_MCP_CHECK_FOR_UPDATES", raising=False)
        monkeypatch.delenv("CI", raising=False)
        monkeypatch.setattr(update_check, "__version__", "4.16.1")
        monkeypatch.setattr(update_check, "_latest_known", None)
        monkeypatch.setattr(update_check, "_read_cache", lambda: None)
        written = {}
        monkeypatch.setattr(
            update_check, "_write_cache", lambda latest: written.update(latest=latest)
        )
        monkeypatch.setattr(update_check, "_fetch_latest_from_pypi", lambda: "4.18.0")

        await refresh_latest_version()

        assert update_check._latest_known == "4.18.0"
        assert written == {"latest": "4.18.0"}

    async def test_fresh_cache_skips_network(self, monkeypatch):
        import time

        monkeypatch.delenv("LINKEDIN_MCP_CHECK_FOR_UPDATES", raising=False)
        monkeypatch.delenv("CI", raising=False)
        monkeypatch.setattr(update_check, "__version__", "4.16.1")
        monkeypatch.setattr(update_check, "_latest_known", None)
        monkeypatch.setattr(
            update_check, "_read_cache", lambda: (time.time(), "4.17.0")
        )
        fetch = MagicMock()
        monkeypatch.setattr(update_check, "_fetch_latest_from_pypi", fetch)

        await refresh_latest_version()

        fetch.assert_not_called()
        assert update_check._latest_known == "4.17.0"


class TestPrimeFromCache:
    def test_seeds_latest_from_fresh_cache(self, monkeypatch):
        import time

        monkeypatch.delenv("LINKEDIN_MCP_CHECK_FOR_UPDATES", raising=False)
        monkeypatch.delenv("CI", raising=False)
        monkeypatch.setattr(update_check, "__version__", "4.16.1")
        monkeypatch.setattr(update_check, "_latest_known", None)
        monkeypatch.setattr(
            update_check, "_read_cache", lambda: (time.time(), "4.18.0")
        )

        prime_from_cache()

        assert update_check._latest_known == "4.18.0"


class TestUpdateNoticeMiddleware:
    async def test_appends_notice_once(self, monkeypatch):
        monkeypatch.setattr(update_check, "__version__", "4.16.1")
        monkeypatch.setattr(update_check, "_latest_known", "4.18.0")
        monkeypatch.setattr(update_check, "prime_from_cache", lambda: None)

        async def _noop() -> None:
            return None

        monkeypatch.setattr(update_check, "refresh_latest_version", _noop)

        middleware = UpdateNoticeMiddleware()

        def fresh_result() -> ToolResult:
            return ToolResult(content=[mt.TextContent(type="text", text="payload")])

        call_next = AsyncMock(side_effect=lambda _ctx: fresh_result())

        first = await middleware.on_call_tool(MagicMock(), call_next)
        assert len(first.content) == 2
        assert isinstance(first.content[-1], mt.TextContent)
        assert "4.18.0" in first.content[-1].text

        # A second call must not nag again.
        second = await middleware.on_call_tool(MagicMock(), call_next)
        assert len(second.content) == 1
