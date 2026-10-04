"""Signing in from cookies handed over through the environment.

Covers the parser (every accepted input format, and the errors a user can act
on) and the gate hook that turns LINKEDIN_COOKIES into a stored session on a
host that cannot open a login window.
"""

from __future__ import annotations

import base64
import json
import time
from unittest.mock import AsyncMock

import pytest

from linkedin_mcp_server import bootstrap
from linkedin_mcp_server.bootstrap import RuntimePolicy
from linkedin_mcp_server.browser_import.env_cookies import (
    COOKIES_ENV,
    COOKIES_FILE_ENV,
    cookie_fingerprint,
    load_env_cookies,
    parse_cookie_input,
)
from linkedin_mcp_server.exceptions import (
    DockerHostLoginRequiredError,
    InvalidCookieInputError,
)
from linkedin_mcp_server.session_state import portable_cookie_path, source_state_path

LI_AT = "AQEDAR-test-li-at-value_123"
FUTURE = time.time() + 30 * 24 * 3600
PAST = time.time() - 3600


def _by_name(cookies):
    return {c.name: c for c in cookies}


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


class TestParseCookieInput:
    def test_bare_li_at_value(self):
        cookies = parse_cookie_input(LI_AT)
        assert [c.name for c in cookies] == ["li_at"]
        li_at = cookies[0]
        assert li_at.value == LI_AT
        assert li_at.domain == ".linkedin.com"
        assert li_at.secure and li_at.http_only
        assert li_at.same_site == "None"
        assert li_at.expires == -1.0

    def test_cookie_header_keeps_quoted_jsessionid(self):
        cookies = _by_name(
            parse_cookie_input(
                f'Cookie: li_at={LI_AT}; JSESSIONID="ajax:123"; bcookie="v=2&abc"'
            )
        )
        assert cookies["li_at"].value == LI_AT
        assert cookies["JSESSIONID"].value == '"ajax:123"'
        assert cookies["bcookie"].value == '"v=2&abc"'

    def test_cookie_editor_export(self):
        raw = json.dumps(
            [
                {
                    "domain": ".www.linkedin.com",
                    "expirationDate": FUTURE,
                    "hostOnly": False,
                    "httpOnly": False,
                    "name": "JSESSIONID",
                    "path": "/",
                    "sameSite": "no_restriction",
                    "secure": True,
                    "session": False,
                    "value": '"ajax:1"',
                },
                {
                    "domain": ".linkedin.com",
                    "expirationDate": FUTURE,
                    "httpOnly": True,
                    "name": "li_at",
                    "path": "/",
                    "sameSite": "no_restriction",
                    "secure": True,
                    "value": LI_AT,
                },
                {"domain": ".example.com", "name": "li_at", "value": "other-site"},
                {
                    "domain": ".linkedin.com",
                    "name": "lang",
                    "session": True,
                    "value": "v=2&lang=tr-tr",
                    "sameSite": "unspecified",
                },
            ]
        )
        cookies = _by_name(parse_cookie_input(raw))
        assert set(cookies) == {"JSESSIONID", "li_at", "lang"}
        assert cookies["li_at"].value == LI_AT
        assert cookies["li_at"].expires == pytest.approx(FUTURE)
        assert cookies["JSESSIONID"].same_site == "None"
        assert cookies["JSESSIONID"].domain == ".www.linkedin.com"
        assert cookies["lang"].expires == -1.0
        assert cookies["lang"].same_site == "Lax"

    def test_playwright_storage_state(self):
        raw = json.dumps(
            {
                "cookies": [
                    {
                        "name": "li_at",
                        "value": LI_AT,
                        "domain": ".linkedin.com",
                        "path": "/",
                        "expires": FUTURE,
                        "httpOnly": True,
                        "secure": True,
                        "sameSite": "None",
                    }
                ],
                "origins": [],
            }
        )
        assert parse_cookie_input(raw)[0].value == LI_AT

    def test_millisecond_expiry_is_normalized(self):
        raw = json.dumps(
            [{"name": "li_at", "value": LI_AT, "expirationDate": FUTURE * 1000}]
        )
        assert parse_cookie_input(raw)[0].expires == pytest.approx(FUTURE)

    def test_base64_prefix(self):
        payload = json.dumps([{"name": "li_at", "value": LI_AT}])
        encoded = base64.b64encode(payload.encode()).decode()
        assert parse_cookie_input(f"base64:{encoded}")[0].value == LI_AT

    def test_unpadded_urlsafe_base64(self):
        encoded = base64.urlsafe_b64encode(f"li_at={LI_AT}".encode()).decode()
        assert parse_cookie_input("BASE64:" + encoded.rstrip("="))[0].value == LI_AT

    def test_missing_li_at_is_an_actionable_error(self):
        with pytest.raises(InvalidCookieInputError, match="no li_at"):
            parse_cookie_input('JSESSIONID="ajax:1"; lang=tr')

    def test_expired_li_at_says_so(self):
        raw = json.dumps([{"name": "li_at", "value": LI_AT, "expires": PAST}])
        with pytest.raises(InvalidCookieInputError, match="expired"):
            parse_cookie_input(raw)

    def test_broken_json_reports_position_not_value(self):
        with pytest.raises(InvalidCookieInputError, match="line 1") as info:
            parse_cookie_input('[{"name": "li_at", "value": "' + LI_AT + '"')
        assert LI_AT not in str(info.value)

    def test_text_with_spaces_is_rejected(self):
        with pytest.raises(InvalidCookieInputError):
            parse_cookie_input("not a cookie at all")

    def test_duplicate_li_at_keeps_registrable_domain(self):
        raw = json.dumps(
            [
                {"name": "li_at", "value": "www-copy", "domain": ".www.linkedin.com"},
                {"name": "li_at", "value": LI_AT, "domain": ".linkedin.com"},
            ]
        )
        cookies = [c for c in parse_cookie_input(raw) if c.name == "li_at"]
        assert [c.value for c in cookies] == [LI_AT]

    def test_fingerprint_follows_li_at_only(self):
        a = parse_cookie_input(f"li_at={LI_AT}; lidc=1")
        b = parse_cookie_input(f"li_at={LI_AT}; lidc=2")
        c = parse_cookie_input("li_at=different")
        assert cookie_fingerprint(a) == cookie_fingerprint(b)
        assert cookie_fingerprint(a) != cookie_fingerprint(c)
        assert LI_AT not in cookie_fingerprint(a)


class TestLoadEnvCookies:
    def test_nothing_set(self):
        assert load_env_cookies() is None

    def test_env_value(self, monkeypatch):
        monkeypatch.setenv(COOKIES_ENV, f"li_at={LI_AT}")
        assert load_env_cookies()[0].value == LI_AT

    def test_file_wins_over_value(self, monkeypatch, tmp_path):
        path = tmp_path / "cookies.json"
        path.write_text(json.dumps([{"name": "li_at", "value": "from-file"}]))
        monkeypatch.setenv(COOKIES_FILE_ENV, str(path))
        monkeypatch.setenv(COOKIES_ENV, f"li_at={LI_AT}")
        assert load_env_cookies()[0].value == "from-file"

    def test_unreadable_file(self, monkeypatch, tmp_path):
        monkeypatch.setenv(COOKIES_FILE_ENV, str(tmp_path / "missing.json"))
        with pytest.raises(InvalidCookieInputError, match="could not be read"):
            load_env_cookies()


# ---------------------------------------------------------------------------
# Gate hook
# ---------------------------------------------------------------------------


def _fake_import(*, accept: bool = True):
    """An import stand-in that writes the files a real accepted import leaves."""
    calls: list[dict] = []

    async def fake(cookies, *, user_data_dir, source_label, superseded_by):
        calls.append({"cookies": cookies, "source": source_label})
        if not accept:
            return False
        user_data_dir.mkdir(parents=True, exist_ok=True)
        (user_data_dir / "Default").mkdir(exist_ok=True)
        (user_data_dir / "Default" / "Cookies").write_text("x")
        portable_cookie_path(user_data_dir).write_text(
            json.dumps([c.to_playwright() for c in cookies])
        )
        source_state_path(user_data_dir).write_text(
            json.dumps(
                {
                    "version": 1,
                    "source_runtime_id": "linux-amd64-container",
                    "login_generation": f"gen-{len(calls)}",
                    "created_at": "2026-10-04T00:00:00Z",
                    "profile_path": str(user_data_dir),
                    "cookies_path": str(portable_cookie_path(user_data_dir)),
                }
            )
        )
        return True

    return fake, calls


@pytest.fixture
def docker_runtime(monkeypatch):
    monkeypatch.setattr(bootstrap, "get_runtime_policy", lambda: RuntimePolicy.DOCKER)
    monkeypatch.setattr(bootstrap, "close_browser", AsyncMock())


def _patch_import(monkeypatch, fake):
    monkeypatch.setattr(
        "linkedin_mcp_server.browser_import.orchestrate.import_session_from_cookies",
        fake,
    )


class TestEnvCookieGate:
    async def test_docker_without_session_signs_in_from_env(
        self, monkeypatch, docker_runtime
    ):
        fake, calls = _fake_import()
        _patch_import(monkeypatch, fake)
        monkeypatch.setenv(COOKIES_ENV, f"li_at={LI_AT}")

        await bootstrap.ensure_tool_ready_or_raise("get_person_profile")

        assert len(calls) == 1
        assert calls[0]["source"] == COOKIES_ENV
        assert bootstrap._auth_ready()

    async def test_same_cookies_are_not_imported_twice(
        self, monkeypatch, docker_runtime
    ):
        fake, calls = _fake_import()
        _patch_import(monkeypatch, fake)
        monkeypatch.setenv(COOKIES_ENV, f"li_at={LI_AT}")

        await bootstrap.ensure_tool_ready_or_raise("t")
        # A restart forgets the in-process memory; the marker on disk does not.
        bootstrap._env_import_tried.clear()
        await bootstrap.ensure_tool_ready_or_raise("t")

        assert len(calls) == 1

    async def test_changed_cookies_replace_the_session(
        self, monkeypatch, docker_runtime
    ):
        fake, calls = _fake_import()
        _patch_import(monkeypatch, fake)
        monkeypatch.setenv(COOKIES_ENV, f"li_at={LI_AT}")
        await bootstrap.ensure_tool_ready_or_raise("t")

        monkeypatch.setenv(COOKIES_ENV, "li_at=a-newer-session")
        await bootstrap.ensure_tool_ready_or_raise("t")

        assert [c["cookies"][0].value for c in calls] == [LI_AT, "a-newer-session"]

    async def test_rejected_cookies_explain_and_are_not_retried(
        self, monkeypatch, docker_runtime
    ):
        fake, calls = _fake_import(accept=False)
        _patch_import(monkeypatch, fake)
        monkeypatch.setenv(COOKIES_ENV, f"li_at={LI_AT}")

        for _ in range(2):
            with pytest.raises(DockerHostLoginRequiredError, match="did not accept"):
                await bootstrap.ensure_tool_ready_or_raise("t")
        assert len(calls) == 1

    async def test_invalid_input_surfaces_in_docker_error(
        self, monkeypatch, docker_runtime
    ):
        fake, calls = _fake_import()
        _patch_import(monkeypatch, fake)
        monkeypatch.setenv(COOKIES_ENV, 'JSESSIONID="ajax:1"')

        with pytest.raises(DockerHostLoginRequiredError, match="no li_at"):
            await bootstrap.ensure_tool_ready_or_raise("t")
        assert calls == []

    async def test_not_configured_keeps_the_hint(self, monkeypatch, docker_runtime):
        with pytest.raises(DockerHostLoginRequiredError, match="LINKEDIN_COOKIES"):
            await bootstrap.ensure_tool_ready_or_raise("t")

    async def test_marker_does_not_vouch_for_a_later_login(
        self, monkeypatch, docker_runtime, isolate_profile_dir
    ):
        fake, calls = _fake_import()
        _patch_import(monkeypatch, fake)
        monkeypatch.setenv(COOKIES_ENV, f"li_at={LI_AT}")
        await bootstrap.ensure_tool_ready_or_raise("t")
        assert bootstrap._read_env_cookie_marker(isolate_profile_dir) is not None

        # A later --login writes a new session generation. The marker was written
        # for the old one, so it must stop claiming the stored session came from
        # the variable.
        state = json.loads(source_state_path(isolate_profile_dir).read_text())
        state["login_generation"] = "manual"
        source_state_path(isolate_profile_dir).write_text(json.dumps(state))
        assert bootstrap._read_env_cookie_marker(isolate_profile_dir) is None
