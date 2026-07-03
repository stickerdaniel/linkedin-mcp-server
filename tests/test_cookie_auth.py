"""Tests for non-interactive cookie auth.

Covers ``cookie_auth`` (parse + seed), the ``--cookie`` / ``LINKEDIN_COOKIE``
config precedence, and the bootstrap cookie-seed branch (success marks ready;
failure raises a clear error and never opens a headed login).
"""

import json
import os
import stat
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from linkedin_mcp_server.cookie_auth import (
    parse_linkedin_cookie,
    seed_session_from_cookie,
)
from linkedin_mcp_server.session_state import portable_cookie_path, source_state_path


class TestParseLinkedInCookie:
    def test_bare_li_at_value(self):
        # A raw value may end with base64 '==' padding; it must not be parsed as
        # a header, so the single cookie keeps the whole value.
        cookies = parse_linkedin_cookie("AQEDAReallyLongValue==")
        assert len(cookies) == 1
        assert cookies[0]["name"] == "li_at"
        assert cookies[0]["value"] == "AQEDAReallyLongValue=="
        assert cookies[0]["domain"] == ".linkedin.com"
        assert cookies[0]["path"] == "/"

    def test_cookie_header_string(self):
        cookies = parse_linkedin_cookie('li_at=abc123; JSESSIONID="ajax:42"; lidc=foo')
        by_name = {c["name"]: c["value"] for c in cookies}
        assert by_name["li_at"] == "abc123"
        assert by_name["JSESSIONID"] == "ajax:42"  # surrounding quotes stripped
        assert by_name["lidc"] == "foo"

    def test_header_with_leading_other_cookie(self):
        cookies = parse_linkedin_cookie("bcookie=v1; li_at=abc")
        assert {c["name"]: c["value"] for c in cookies} == {
            "bcookie": "v1",
            "li_at": "abc",
        }

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            parse_linkedin_cookie("   ")

    def test_header_without_li_at_raises(self):
        with pytest.raises(ValueError):
            parse_linkedin_cookie("JSESSIONID=ajax:1; bcookie=v")

    def test_li_at_empty_value_raises(self):
        with pytest.raises(ValueError):
            parse_linkedin_cookie("li_at=; bcookie=v")


class TestSeedSessionFromCookie:
    @pytest.mark.asyncio
    async def test_success_persists_source_state(
        self, isolate_profile_dir, monkeypatch
    ):
        user_data_dir = isolate_profile_dir
        monkeypatch.setattr(
            "linkedin_mcp_server.drivers.browser.validate_imported_cookies",
            AsyncMock(return_value=True),
        )

        ok = await seed_session_from_cookie("li_at=abc; JSESSIONID=x", user_data_dir)

        assert ok is True
        cookie_path = portable_cookie_path(user_data_dir)
        assert cookie_path.exists()
        written = json.loads(cookie_path.read_text())
        assert {c["name"] for c in written} == {"li_at", "JSESSIONID"}
        if os.name != "nt":
            assert stat.S_IMODE(cookie_path.stat().st_mode) == 0o600
        assert source_state_path(user_data_dir).exists()

    @pytest.mark.asyncio
    async def test_rejected_cookie_cleans_up(self, isolate_profile_dir, monkeypatch):
        user_data_dir = isolate_profile_dir
        monkeypatch.setattr(
            "linkedin_mcp_server.drivers.browser.validate_imported_cookies",
            AsyncMock(return_value=False),
        )

        ok = await seed_session_from_cookie("rawlivalue", user_data_dir)

        assert ok is False
        assert not portable_cookie_path(user_data_dir).exists()
        assert not source_state_path(user_data_dir).exists()

    @pytest.mark.asyncio
    async def test_malformed_cookie_raises_before_browser(self, isolate_profile_dir):
        # A multi-cookie header without li_at must fail before any browser launch.
        with pytest.raises(ValueError):
            await seed_session_from_cookie(
                "JSESSIONID=x; bcookie=y", isolate_profile_dir
            )


class TestCookieConfigPrecedence:
    def test_cli_cookie_sets_server_cookie(self, monkeypatch):
        monkeypatch.setattr("sys.argv", ["linkedin-mcp-server", "--cookie", "AQEDval"])
        monkeypatch.delenv("LINKEDIN_COOKIE", raising=False)
        from linkedin_mcp_server.config import get_config

        assert get_config().server.cookie == "AQEDval"

    def test_env_cookie_fallback(self, monkeypatch):
        monkeypatch.setattr("sys.argv", ["linkedin-mcp-server"])
        monkeypatch.setenv("LINKEDIN_COOKIE", "envvalue")
        from linkedin_mcp_server.config import get_config

        assert get_config().server.cookie == "envvalue"

    def test_cli_overrides_env(self, monkeypatch):
        monkeypatch.setattr("sys.argv", ["linkedin-mcp-server", "--cookie", "argval"])
        monkeypatch.setenv("LINKEDIN_COOKIE", "envvalue")
        from linkedin_mcp_server.config import get_config

        assert get_config().server.cookie == "argval"

    def test_default_cookie_is_none(self, monkeypatch):
        monkeypatch.setattr("sys.argv", ["linkedin-mcp-server"])
        monkeypatch.delenv("LINKEDIN_COOKIE", raising=False)
        from linkedin_mcp_server.config import get_config

        assert get_config().server.cookie is None


class TestBootstrapCookieSeed:
    @pytest.mark.asyncio
    async def test_seed_failure_raises_and_no_headed_login(self, monkeypatch):
        import linkedin_mcp_server.bootstrap as bootstrap
        from linkedin_mcp_server.core.exceptions import AuthenticationError

        monkeypatch.setattr(
            bootstrap,
            "get_config",
            lambda: SimpleNamespace(server=SimpleNamespace(cookie="li_at=expired")),
        )
        monkeypatch.setattr(bootstrap, "_auth_ready", lambda: False)
        seed = AsyncMock(return_value=False)
        monkeypatch.setattr(bootstrap, "_try_seed_from_cookie", seed)

        with pytest.raises(AuthenticationError):
            await bootstrap._seed_cookie_or_raise(None)

        seed.assert_awaited_once()
        # The cookie path must never fall through to an interactive headed login.
        assert bootstrap.get_bootstrap_state().login_task is None

    @pytest.mark.asyncio
    async def test_seed_success_marks_ready(self, monkeypatch):
        import linkedin_mcp_server.bootstrap as bootstrap

        monkeypatch.setattr(
            bootstrap,
            "get_config",
            lambda: SimpleNamespace(server=SimpleNamespace(cookie="rawvalue")),
        )
        # Not ready when claiming, ready after the seed persists artifacts.
        ready = iter([False, True])
        monkeypatch.setattr(bootstrap, "_auth_ready", lambda: next(ready, True))
        seed = AsyncMock(return_value=True)
        monkeypatch.setattr(bootstrap, "_try_seed_from_cookie", seed)

        await bootstrap._seed_cookie_or_raise(None)  # must not raise

        seed.assert_awaited_once()
        assert bootstrap.get_bootstrap_state().auth_state == bootstrap.AuthState.READY

    @pytest.mark.asyncio
    async def test_seed_runs_once_under_concurrency(self, monkeypatch):
        import asyncio

        import linkedin_mcp_server.bootstrap as bootstrap

        monkeypatch.setattr(
            bootstrap,
            "get_config",
            lambda: SimpleNamespace(server=SimpleNamespace(cookie="rawvalue")),
        )
        monkeypatch.setattr(bootstrap, "_auth_ready", lambda: False)

        calls = 0

        async def fake_seed(_value):
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.01)
            return False

        monkeypatch.setattr(bootstrap, "_try_seed_from_cookie", fake_seed)

        results = await asyncio.gather(
            *(bootstrap._seed_cookie_or_raise(None) for _ in range(4)),
            return_exceptions=True,
        )

        # One shared seed task; every concurrent caller still gets the clear error.
        assert calls == 1
        assert all(isinstance(r, Exception) for r in results)
